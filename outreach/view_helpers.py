"""Pure presentation helpers for the Streamlit dashboard (no Streamlit import, so they are unit-testable)."""
from __future__ import annotations

import html
import json

import pandas as pd

NA = "Not Available"

_EMAIL_LABELS = {"not_contacted": "Not contacted", "simulated": "Simulated (not sent)", "sent": "Sent",
                 "failed": "Failed", "skipped": "Skipped", "pending": "In progress"}
_DM_LABELS = {**_EMAIL_LABELS, "simulated": "Simulated", "sent": "Sent manually"}


def status_label(channel: str, status: str | None) -> str:
    labels = _DM_LABELS if channel == "instagram_dm" else _EMAIL_LABELS
    return labels.get(status or "not_contacted", (status or "").replace("_", " ").capitalize())


def has_message(row: dict) -> bool:
    msg = row.get("message")
    return bool(msg and (msg.get("email") or "").strip() and (msg.get("dm") or "").strip())


def pick_default_creator(rows: list[dict]) -> str | None:
    """Prefer a qualified creator with a generated message, then any creator with one, then the first qualified."""
    if not rows:
        return None
    for pred in (lambda r: r["filter_status"] == "PASS" and has_message(r), has_message,
                 lambda r: r["filter_status"] == "PASS"):
        for r in rows:
            if pred(r):
                return r["channel_id"]
    return rows[0]["channel_id"]


def engagement_pct(row: dict) -> float | None:
    rate = row.get("engagement_rate")
    return round(rate * 100, 2) if isinstance(rate, (int, float)) else None


def creators_frame(rows: list[dict]) -> pd.DataFrame:
    """Display table. Numeric columns stay numeric (missing = empty), so Arrow can serialise them."""
    cols = ["Name", "Platform", "Profile", "Niche", "Followers", "Engagement (%)", "Qualification",
            "Contact email", "Message", "Email status", "DM status"]
    df = pd.DataFrame({
        "Name": [r["name"] for r in rows], "Platform": [r["platform"] for r in rows],
        "Profile": [r["profile_url"] for r in rows], "Niche": [r.get("niche") or "" for r in rows],
        "Followers": pd.array([r.get("follower_count") for r in rows], dtype="Int64"),
        "Engagement (%)": pd.Series([engagement_pct(r) for r in rows], dtype="float64"),
        "Qualification": ["Qualified" if r["filter_status"] == "PASS" else "Not qualified" for r in rows],
        "Contact email": [r.get("contact_email") or "Not Found" for r in rows],
        "Message": ["Ready" if has_message(r) else "Not yet" for r in rows],
        "Email status": [status_label("email", r.get("email_status")) for r in rows],
        "DM status": [status_label("instagram_dm", r.get("dm_status")) for r in rows],
    })
    return df[cols]


def message_origin(msg: dict) -> tuple[str, str]:
    """(label, colour) saying honestly where a message came from."""
    source = msg.get("source")
    if source == "llm":
        return "AI-generated", "green"
    if source == "fallback_template":
        return "Standard template", "orange"
    return "Source unknown", "gray"


def message_notice(msg: dict) -> str | None:
    if msg.get("source") != "fallback_template":
        return None
    why = ("there was not enough verified information about this creator for AI personalisation"
           if msg.get("validation_status") == "fallback_insufficient_context"
           else "the AI draft did not pass the length and quality checks")
    return (f"This is a standard template, not an AI-personalised message, because {why}. It only uses "
            "verified facts, so it is accurate but less personal. Review it before use.")


_STATUS_COLOURS = {"not_contacted": "gray", "simulated": "blue", "sent": "green", "failed": "red",
                   "skipped": "orange", "pending": "orange"}


def status_badge(channel: str, status: str | None) -> str:
    """Streamlit markdown badge, e.g. ':blue-badge[Simulated (not sent)]'."""
    return f":{_STATUS_COLOURS.get(status or 'not_contacted', 'gray')}-badge[{status_label(channel, status)}]"


def funnel_rows(stats: dict) -> list[tuple[str, int]]:
    out = stats.get("outreach") or {}
    handled = out.get("email:simulated", 0) + out.get("email:sent", 0)
    return [("Discovered", stats["influencers"]), ("Qualified", stats["qualified"]),
            ("With email", stats["qualified_with_email"]), ("Message ready", stats["qualified_with_message"]),
            ("Emailed or simulated", handled)]


def outreach_counts(stats: dict) -> dict:
    out = stats.get("outreach") or {}
    e = {k: out.get(f"email:{k}", 0) for k in ("simulated", "sent", "failed", "skipped")}
    d = {k: out.get(f"instagram_dm:{k}", 0) for k in ("sent", "failed", "skipped")}
    e["waiting"] = max(stats["qualified_with_email"] - sum(e.values()), 0)
    d["waiting"] = max(stats["qualified_with_message"] - sum(d.values()), 0)
    return {"email": e, "dm": d}


def copy_button_html(text: str, label: str) -> str:
    """Self-contained copy-to-clipboard button for components.html (falls back to execCommand)."""
    payload = json.dumps(text).replace("</", "<\\/")
    return f"""<style>body{{margin:0}}button{{font:600 13px system-ui,sans-serif;color:#E6EAF2;background:#22305A;
border:1px solid #5B8DEF;border-radius:8px;padding:6px 14px;cursor:pointer}}button:hover{{background:#2C3F75}}
button.ok{{background:#14402F;border-color:#2FA37A}}</style>
<button id="b">{html.escape(label)}</button>
<script>
const text = {payload}, label = {json.dumps(label)}, b = document.getElementById('b');
function done(ok) {{ b.textContent = ok ? 'Copied' : 'Copy failed - select the text and copy it manually';
  b.className = ok ? 'ok' : ''; setTimeout(() => {{ b.textContent = label; b.className = ''; }}, 2200); }}
b.onclick = async () => {{
  try {{ await navigator.clipboard.writeText(text); done(true); }}
  catch (e) {{ const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select();
    let ok = false; try {{ ok = document.execCommand('copy'); }} catch (e2) {{}} document.body.removeChild(t); done(ok); }}
}};
</script>"""


def explain_error(detail: str | None) -> str:
    text = (detail or "").lower()
    if "already" in text or "duplicate" in text:
        return "This outreach was already recorded, so it was not repeated."
    if "confirm" in text:
        return "Please tick the confirmation box before sending real emails."
    if "not found" in text or "404" in text:
        return "That creator could not be found. Refresh the data and try again."
    return "Something went wrong and nothing was changed. Open the details below if you need to report it."


def error_text(detail: str | None) -> str:
    """Friendly sentence plus the API's own (short, user-facing) message, so the specific reason stays visible."""
    base, d = explain_error(detail), (detail or "").strip()
    if d and len(d) <= 200 and not d.startswith("Cannot reach"):
        return f"{base} ({d})"
    return base


def describe_run(res: dict) -> tuple[str, str]:
    """(streamlit_kind, message) for a batch email run. Never claims delivery in simulation mode."""
    real = res.get("mode") in ("smtp", "real")
    out = {k: v for k, v in (res.get("outcomes") or {}).items() if v}
    shown = "sent" if real else "simulated"
    n = out.pop(shown, 0)
    text = (f"{n} email(s) sent through your SMTP provider." if real
            else f"Simulation finished: {n} email(s) recorded as simulated. No emails were sent.")
    if out:
        text += " Other results: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in out.items()) + "."
    if res.get("aborted"):
        return "warning", f"{text} The run stopped early: {res['aborted']}"
    if not n and not out:
        return "info", "There was nothing to process: every eligible creator has already been handled."
    return ("success" if n else "info"), text


def describe_email_result(res: dict) -> tuple[str, str, str | None]:
    """(kind, message, technical_detail) for a single-creator email action."""
    status, real, err = res.get("status"), res.get("mode") in ("smtp", "real"), res.get("error")
    if status == "simulated":
        return "success", "Email simulated and recorded in the tracker; no email was sent.", None
    if status == "sent" and real:
        return "success", "The email was handed to your SMTP provider.", None
    if status == "failed":
        return "error", "The email could not be sent. Nothing was delivered.", err
    if status == "skipped":
        return "info", "Skipped: this creator has no usable contact email or message.", err
    return "warning", f"The outreach ended with status '{status}'. Check the history below.", err
