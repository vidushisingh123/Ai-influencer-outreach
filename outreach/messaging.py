"""Phase 2: personalized email + Instagram DM generation for qualified influencers.

Run:  python -m outreach.messaging
Reads data/influencers_all.json (Phase 1), writes data/messages.json. Nothing is sent.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .llm_service import (GroqService, LLMFatalError, LLMResponseError, LLMTransientError,
                          MissingAPIKeyError)

log = logging.getLogger("outreach.messaging")

EMAIL_WORDS = (60, 90)
DM_WORDS = (15, 30)
MAX_ATTEMPTS = 3
USABLE = {"valid", "fallback_insufficient_context", "fallback_after_invalid_output"}
REUSABLE = {"valid"}


@dataclass(frozen=True)
class Sender:
    brand: str
    sender_name: str
    collab_type: str = "UGC content creation"
    brand_description: str = ""


SYSTEM_PROMPT = """You write short, honest brand-collaboration outreach for a marketing team.
Reply with ONLY a JSON object: {"subject": "...", "email": "...", "dm": "..."}

Rules:
- email: plain text, 60-90 words INCLUDING greeting and sign-off. Aim for 75-80 words. Write a complete, natural, personalized collaboration email.
- dm: casual Instagram DM, 15-30 words. Aim for 20-25 words. Make it sound natural and conversational.
- subject: at most 10 words.
- Use ONLY facts present in the JSON you are given. Never invent recent posts, audience demographics, follower counts, previous relationships with the brand, personal details, prices, free products, timelines or results.
- Refer to a recent video only by its provided title. Use double quotation marks only to quote a provided title exactly; use no other quotation marks.
- Address the creator by the provided name exactly; do not guess a first name.
- Do not claim to have watched or loved specific content; say you noticed or came across it.
- The value proposition may only use facts from brand_description. If it is empty, keep it to the collaboration itself without promising specifics.
- No placeholders, brackets or links."""


# ------------------------------------------------------------------ context + prompt
def build_context(rec: dict) -> dict:
    """Only verified Phase 1 facts are passed to the model (no email, description or metrics)."""
    titles = [v["title"] for v in (rec.get("recent_videos") or [])[:3] if v.get("title")]
    return {"creator_name": rec["name"], "platform": rec.get("platform", "YouTube"), "niche": rec["niche"],
            "content_themes": rec.get("content_themes") or [], "recent_video_titles": titles}


def has_enough_context(ctx: dict) -> bool:
    return bool(ctx["content_themes"] or ctx["recent_video_titles"])


def build_user_prompt(sender: Sender, ctx: dict, feedback: str | None = None) -> str:
    payload = {"sender": {"name": sender.sender_name, "brand": sender.brand,
                          "brand_description": sender.brand_description, "collaboration_type": sender.collab_type},
               "creator": ctx}
    prompt = json.dumps(payload, ensure_ascii=False, indent=2)
    return prompt + (f"\n\nFix this and answer again: {feedback}" if feedback else "")


# ------------------------------------------------------------------ parsing + validation
def count_words(text: str) -> int:
    return len(text.split())


def parse_response(text: str) -> dict[str, str]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    obj = json.loads(cleaned[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    out = {}
    for key in ("subject", "email", "dm"):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            raise ValueError(f"missing or empty '{key}'")
        out[key] = obj[key].strip()
    return out


def validate_messages(subject: str, email: str, dm: str, titles: list[str]) -> list[str]:
    problems = []
    ne, nd = count_words(email), count_words(dm)
    if not EMAIL_WORDS[0] <= ne <= EMAIL_WORDS[1]:
        problems.append(f"email has {ne} words (required {EMAIL_WORDS[0]}-{EMAIL_WORDS[1]})")
    if not DM_WORDS[0] <= nd <= DM_WORDS[1]:
        problems.append(f"DM has {nd} words (required {DM_WORDS[0]}-{DM_WORDS[1]})")
    if count_words(subject) > 12:
        problems.append("subject is longer than 12 words")
    for label, text in (("email", email), ("dm", dm)):
        if re.search(r"[\[\]{}<>]|https?://", text):
            problems.append(f"{label} contains a placeholder, bracket or link")
        for quote in re.findall(r'"([^"]+)"|“([^”]+)”', text):
            q = next(part for part in quote if part)
            if count_words(q) >= 2 and not any(q.lower() in t.lower() for t in titles):
                problems.append(f'{label} quotes text that is not a provided video title: "{q}"')
    return problems


# ------------------------------------------------------------------ fallback (deterministic, facts only)
def _natural_list(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c' - theme names read naturally ('Haircare & styling' -> 'haircare and styling')."""
    compound = any(" & " in i for i in items)          # avoid "haircare and styling and skincare"
    items = [i.lower().replace(" & ", " and ") for i in items]
    if len(items) == 1:
        return items[0]
    return " plus ".join(items) if compound and len(items) == 2 else ", ".join(items[:-1]) + " and " + items[-1]


def build_fallback(ctx: dict, sender: Sender) -> tuple[str, str, str]:
    """Deterministic template used when the model output is unusable. Facts only: creator name, platform,
    detected content themes, brand and sender. It never claims to have watched or liked specific content."""
    themes = ctx["content_themes"][:2]
    focus = _natural_list(themes) if themes else ctx["niche"].lower().replace(" & ", " and ")
    name, me, brand, collab = ctx["creator_name"], sender.sender_name, sender.brand, sender.collab_type
    email = (
        f"Hi {name},\n\nI'm {me} from {brand}. I noticed your {focus} content on {ctx['platform']} and thought "
        f"you could be a great fit for a {collab} collaboration with us. We'd be glad to share the details, "
        f"including format and terms, so you can decide whether it suits you. There's no pressure at all if it "
        f"isn't the right fit. Would you be open to a short reply so we can take it from there?"
        f"\n\nBest regards,\n{me}\n{brand}")
    dm_options = [
        f"Hi {name}, I'm {me} from {brand}. I noticed your {focus} content and we'd love to chat about a "
        f"{collab} collaboration. Would you be open to hearing more?",
        f"Hi {name}, I'm {me} from {brand}. We'd love to chat about a {collab} collaboration with you. "
        f"Would you be open to it?",
        f"Hi {name}, {me} from {brand} here. We'd love to chat about a {collab} collaboration. Interested?",
        f"Hi {name}, we'd love to chat about a collaboration with {brand}. Would you be open to hearing more?",
    ]
    dm = next((d for d in dm_options if DM_WORDS[0] <= count_words(d) <= DM_WORDS[1]), dm_options[-1])
    return f"Collaboration idea for {name}", email, dm


# ------------------------------------------------------------------ generation
def _result(rec: dict, model: str | None, *, subject=None, email=None, dm=None, source=None,
            status: str, attempts: int, error: str | None = None) -> dict:
    return {
        "channel_id": rec["channel_id"], "name": rec["name"],
        "contact_email": rec.get("contact_email", "Not Found"),
        "subject": subject, "email": email, "dm": dm,
        "email_word_count": count_words(email) if email else None,
        "dm_word_count": count_words(dm) if dm else None,
        "source": source, "validation_status": status, "attempts": attempts, "model": model,
        "error": error, "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fallback_result(rec: dict, ctx: dict, sender: Sender, model, status: str, attempts: int, error: str) -> dict:
    subject, email, dm = build_fallback(ctx, sender)
    problems = validate_messages(subject, email, dm, [])
    if problems:  # e.g. an extremely long creator name; never ship an out-of-limit message
        return _result(rec, model, status="invalid", attempts=attempts, error=f"{error}; fallback invalid: {problems}")
    return _result(rec, model, subject=subject, email=email, dm=dm, source="fallback_template",
                   status=status, attempts=attempts, error=error)


def generate_for(rec: dict, sender: Sender, service, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """One influencer -> one result dict. Raises only LLMTransientError / LLMFatalError."""
    ctx = build_context(rec)
    model = getattr(service, "model", None)
    if not has_enough_context(ctx):
        return _fallback_result(rec, ctx, sender, model, "fallback_insufficient_context", 0,
                                "insufficient verified context (no content themes or recent video titles)")
    feedback, last_problem = None, "no attempts made"
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = parse_response(service.complete(SYSTEM_PROMPT, build_user_prompt(sender, ctx, feedback)))
        except (LLMResponseError, ValueError) as exc:   # JSONDecodeError is a ValueError
            last_problem = f"malformed model output: {exc}"
            feedback = f"your previous reply was unusable ({exc}). Reply with ONLY the JSON object."
            continue
        problems = validate_messages(parsed["subject"], parsed["email"], parsed["dm"], ctx["recent_video_titles"])
        if not problems:
            return _result(rec, model, subject=parsed["subject"], email=parsed["email"], dm=parsed["dm"],
                           source="llm", status="valid", attempts=attempt)
        last_problem = "; ".join(problems)
        feedback = f"the previous attempt failed validation: {last_problem}."
    return _fallback_result(rec, ctx, sender, model, "fallback_after_invalid_output", max_attempts, last_problem)


# ------------------------------------------------------------------ batch runner
def _load_existing(path: Path) -> dict[str, dict]:
    try:
        return {m["channel_id"]: m for m in json.loads(Path(path).read_text())["messages"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save(path: Path, messages: list[dict], model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "model": model,
               "word_limits": {"email": list(EMAIL_WORDS), "dm": list(DM_WORDS)}, "messages": messages}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
      json.dumps(payload, indent=2, ensure_ascii=False),
      encoding="utf-8"
    )
    tmp.replace(path)


def run(input_path: Path, output_path: Path, sender: Sender, service, *, limit: int | None = None,
        force: bool = False, delay: float = 0.0, sleep=time.sleep, max_attempts: int = MAX_ATTEMPTS,
        max_consecutive_errors: int = 3) -> dict:
    records = json.loads(Path(input_path).read_text(encoding="utf-8"))
    qualified = [r for r in records if r.get("filter_status") == "PASS"]   # failed influencers never get messages
    order = [r["channel_id"] for r in qualified]
    results = {} if force else {c: m for c, m in _load_existing(output_path).items()
                                if m.get("validation_status") in REUSABLE and c in order}
    reused = len(results)
    todo = [r for r in qualified if r["channel_id"] not in results]
    if limit is not None:
        todo = todo[:limit]

    def save() -> None:
        _save(output_path, [results[c] for c in order if c in results], getattr(service, "model", None))

    aborted, consecutive, generated = None, 0, 0
    for rec in todo:
        try:
            result = generate_for(rec, sender, service, max_attempts)
            consecutive = 0
        except LLMTransientError as exc:
            result = _result(rec, getattr(service, "model", None), status="error", attempts=max_attempts, error=str(exc))
            consecutive += 1
        except LLMFatalError as exc:
            aborted = f"fatal API error, run stopped: {exc}"
            break
        results[rec["channel_id"]] = result
        generated += 1
        save()
        if consecutive >= max_consecutive_errors:
            aborted = f"{consecutive} consecutive API failures, run stopped (re-run later to resume)"
            break
        if delay:
            sleep(delay)
    save()
    return {"qualified": len(qualified), "skipped_failed": len(records) - len(qualified),
            "generated_this_run": generated, "reused_existing": reused,
            "by_status": dict(Counter(m["validation_status"] for m in results.values())), "aborted": aborted}


class OnDemandError(RuntimeError):
    """A single-creator generation request that cannot run (unknown/unqualified creator, unreadable data)."""


def generate_one(channel_id: str, input_path: Path, output_path: Path, sender: Sender, service_factory, *,
                 force: bool = False, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Generate (or reuse) the message for exactly ONE qualified creator and store it in messages.json.

    Reuses generate_for (validation, retries, fallback). A valid AI message is reused without calling the API
    unless force=True, and a new non-valid result never replaces an existing valid one. Other creators' entries
    are written back untouched. outcome: 'reused' | 'generated' | 'fallback' | 'kept_existing'.
    Raises OnDemandError, LLMTransientError or LLMFatalError (nothing is saved in those cases)."""
    try:
        records = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OnDemandError(f"creator data could not be read ({Path(input_path).name})") from exc
    rec = next((r for r in records if r.get("channel_id") == channel_id), None)
    if rec is None:
        raise OnDemandError("this creator is not in the discovered creator data")
    if rec.get("filter_status") != "PASS":
        raise OnDemandError("messages are only generated for qualified creators")
    existing = _load_existing(output_path)
    current = existing.get(channel_id)
    has_valid = bool(current) and current.get("validation_status") in REUSABLE
    if has_valid and not force:
        return {"outcome": "reused", "message": current}
    service = service_factory()
    result = generate_for(rec, sender, service, max_attempts)
    if result["validation_status"] not in REUSABLE and has_valid:
        return {"outcome": "kept_existing", "message": current, "error": result["error"]}
    existing[channel_id] = result
    _save(Path(output_path), list(existing.values()), getattr(service, "model", None))
    return {"outcome": "generated" if result["validation_status"] in REUSABLE else "fallback", "message": result}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate personalized email + Instagram DM for qualified influencers")
    p.add_argument("--input", type=Path, default=config.DATA_DIR / "influencers_all.json")
    p.add_argument("--output", type=Path, default=config.DATA_DIR / "messages.json")
    p.add_argument("--limit", type=int, default=None, help="generate for at most N influencers this run")
    p.add_argument("--force", action="store_true", help="regenerate even if a usable message already exists")
    p.add_argument("--delay", type=float, default=0.0, help="seconds to pause between influencers")
    p.add_argument("--model", default=None, help="override GROQ_MODEL")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not config.BRAND_NAME or not config.SENDER_NAME:
        print("Configuration error: set BRAND_NAME and SENDER_NAME in .env (see .env.example)")
        return 2
    if not args.input.exists():
        print(f"Input not found: {args.input}. Run Phase 1 first: python -m outreach.pipeline")
        return 1
    try:
        service = GroqService(model=args.model)
    except MissingAPIKeyError as exc:
        print(f"Configuration error: {exc}")
        return 2
    sender = Sender(config.BRAND_NAME, config.SENDER_NAME, config.COLLAB_TYPE, config.BRAND_DESCRIPTION)
    summary = run(args.input, args.output, sender, service, limit=args.limit, force=args.force, delay=args.delay)
    print(json.dumps(summary, indent=2))
    print(f"Saved: {args.output}")
    return 1 if summary["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
