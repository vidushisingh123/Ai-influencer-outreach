"""Business logic: import Phase 1/2 files, send (or simulate) emails, record manual DM outcomes, export tracker."""
from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from pathlib import Path

from . import config, db
from .messaging import USABLE
from .sender import SendError, validate_recipient

log = logging.getLogger(__name__)


class NotFound(LookupError):
    """Unknown influencer."""


class OutreachError(ValueError):
    """The requested action is not allowed (bad input, nothing to send, ...)."""


def _load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OutreachError(f"cannot read {what} file {path}: {exc.strerror or exc}") from None
    except ValueError:
        raise OutreachError(f"{what} file {path} is not valid JSON") from None


# ---------------------------------------------------------------- import
def import_from_files(conn, influencers_path: Path | None = None, messages_path: Path | None = None) -> dict:
    inf_path = Path(influencers_path or config.DATA_DIR / "influencers_all.json")
    msg_path = Path(messages_path or config.DATA_DIR / "messages.json")
    if not inf_path.exists():
        raise OutreachError(f"{inf_path} not found - run Phase 1 first (python -m outreach.pipeline)")
    records = _load_json(inf_path, "influencers")
    if not isinstance(records, list):
        raise OutreachError(f"{inf_path} must contain a list of influencer records")

    counts = {"influencers": 0, "qualified": 0, "invalid_records": 0, "messages": 0, "messages_ignored": 0}
    status_by_id: dict[str, str] = {}
    for rec in records:
        ok = (isinstance(rec, dict) and rec.get("channel_id") and rec.get("name")
              and str(rec.get("profile_url", "")).startswith("https://") and rec.get("filter_status") in ("PASS", "FAIL"))
        if not ok:
            counts["invalid_records"] += 1
            continue
        db.upsert_influencer(conn, rec)
        status_by_id[rec["channel_id"]] = rec["filter_status"]
        counts["influencers"] += 1
        counts["qualified"] += rec["filter_status"] == "PASS"

    if not msg_path.exists():
        counts["warning"] = f"{msg_path} not found - run Phase 2 (python -m outreach.messaging)"
        return counts
    data = _load_json(msg_path, "messages")
    for msg in (data.get("messages", []) if isinstance(data, dict) else []):
        cid = msg.get("channel_id") if isinstance(msg, dict) else None
        if status_by_id.get(cid) != "PASS":        # failed influencers never get outreach messages
            counts["messages_ignored"] += 1
            continue
        db.upsert_message(conn, msg)
        counts["messages"] += 1
    return counts


# ---------------------------------------------------------------- email
def _outcome(channel_id: str, status: str, mode: str, recipient: str | None = None, error: str | None = None) -> dict:
    return {"channel_id": channel_id, "status": status, "mode": mode, "recipient": recipient, "error": error}


def send_email(conn, sender, channel_id: str) -> dict:
    """Outcome status is one of: sent, simulated, failed, skipped, duplicate."""
    inf = db.get_influencer(conn, channel_id)
    if inf is None:
        raise NotFound(f"unknown influencer {channel_id!r}")
    msg, to, mode = inf["message"], inf["contact_email"], sender.mode

    def skip(reason: str) -> dict:
        db.record_skipped(conn, channel_id, "email", to, mode, reason, msg["subject"] if msg else None)
        return _outcome(channel_id, "skipped", mode, to, reason)

    if inf["filter_status"] != "PASS":
        return skip("not qualified (filter_status=FAIL)")
    if not msg or msg["validation_status"] not in USABLE or not msg.get("email") or not msg.get("subject"):
        return skip("no usable generated message")
    reason = validate_recipient(to)
    if reason:
        return skip(reason)
    to = to.strip()
    duplicate = _outcome(channel_id, "duplicate", mode, to, "already contacted - nothing sent")

    if mode == "simulation":
        if db.has_active(conn, channel_id, "email"):       # already really sent: do not re-simulate
            return duplicate
        try:
            db.insert_outreach(conn, channel_id, "email", to, "simulation", "simulated", msg["subject"], msg["email"])
        except db.DuplicateOutreach:
            return duplicate
        return _outcome(channel_id, "simulated", mode, to)

    try:    # real sending: claim first (unique index), send second, record third
        row_id = db.insert_outreach(conn, channel_id, "email", to, mode, "pending", msg["subject"], msg["email"])
    except db.DuplicateOutreach:
        return duplicate
    try:
        sender.send(to, msg["subject"], msg["email"])
    except SendError as exc:
        db.finish_outreach(conn, row_id, "failed", str(exc))
        return _outcome(channel_id, "failed", mode, to, str(exc))
    except Exception as exc:  # noqa: BLE001 - never leave a claim hanging on an unexpected error
        log.exception("Unexpected error while sending to influencer %s", channel_id)
        db.finish_outreach(conn, row_id, "failed", f"unexpected error: {type(exc).__name__}")
        return _outcome(channel_id, "failed", mode, to, f"unexpected error: {type(exc).__name__}")
    try:
        db.finish_outreach(conn, row_id, "sent")
    except db.DatabaseError:
        log.critical("Email to influencer %s WAS SENT but the status update failed; row stays 'pending' "
                     "so it cannot be sent twice", channel_id)
        return _outcome(channel_id, "sent", mode, to, "sent, but the status update failed (row left pending)")
    return _outcome(channel_id, "sent", mode, to)


def run_email_batch(conn, sender, limit: int | None = None, max_consecutive_failures: int = 3) -> dict:
    candidates = [i["channel_id"] for i in db.list_influencers(conn, qualification="PASS") if i["message"]]
    counts: Counter = Counter()
    attempted = consecutive = 0
    aborted = None
    for cid in candidates:
        if limit is not None and attempted >= limit:
            break
        out = send_email(conn, sender, cid)
        counts[out["status"]] += 1
        if out["status"] in ("sent", "simulated", "failed"):
            attempted += 1
        if out["status"] == "failed":          # skipped/duplicate rows involve no send, so they do not reset this
            consecutive += 1
        elif out["status"] in ("sent", "simulated"):
            consecutive = 0
        if consecutive >= max_consecutive_failures:
            aborted = f"{consecutive} consecutive send failures - run stopped"
            break
    return {"mode": sender.mode, "candidates": len(candidates), "outcomes": dict(counts), "aborted": aborted}


# ---------------------------------------------------------------- Instagram DM (always manual)
def record_dm(conn, channel_id: str, status: str, note: str | None = None) -> dict:
    """The operator sends the DM themselves in Instagram, then records the outcome here."""
    if status not in ("sent", "failed"):
        raise OutreachError("status must be 'sent' or 'failed'")
    inf = db.get_influencer(conn, channel_id)
    if inf is None:
        raise NotFound(f"unknown influencer {channel_id!r}")
    msg = inf["message"]
    if inf["filter_status"] != "PASS":
        raise OutreachError("influencer is not qualified")
    if not msg or msg["validation_status"] not in USABLE or not msg.get("dm"):
        raise OutreachError("no usable generated DM for this influencer")
    note = (note or "").strip()[:500] or None
    try:
        row_id = db.insert_outreach(conn, channel_id, "instagram_dm", inf["instagram_url"], "manual", status,
                                    None, msg["dm"], note if status == "failed" else None,
                                    note if status == "sent" else None)
    except db.DuplicateOutreach:
        raise db.DuplicateOutreach("this DM is already marked as sent") from None
    return {"id": row_id, "channel_id": channel_id, "status": status, "mode": "manual"}


# ---------------------------------------------------------------- tracker export
def export_tracker(conn, path: Path) -> int:
    """CSV outreach tracker for qualified influencers (assignment section 7D)."""
    rows = []
    for i in db.list_influencers(conn, qualification="PASS"):
        e, d = i["email_outreach"], i["dm_outreach"]
        done = lambda o: o["updated_at"] if o and o["status"] in ("sent", "simulated") else ""  # noqa: E731
        rows.append({
            "Influencer": i["name"], "Email": i["contact_email"],
            "Message Generated": "Yes" if i["message"] else "No",
            "Message Source": (i["message"] or {}).get("source") or "",
            "Email Status": i["email_status"], "Email Mode": e["mode"] if e else "", "Email Sent Date": done(e),
            "Email Error": (e or {}).get("error") or "",
            "DM Status": i["dm_status"], "DM Sent Date": done(d), "Instagram": i["instagram_url"] or "Not Found",
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["Influencer", "Email", "Message Generated", "Message Source", "Email Status", "Email Mode",
              "Email Sent Date", "Email Error", "DM Status", "DM Sent Date", "Instagram"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
