"""Streamlit dashboard (talks to the FastAPI backend only).

Run:  streamlit run outreach/dashboard.py     (start the API first: uvicorn outreach.api:create_app --factory)
"""
import os
import sys
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from streamlit.components.v1 import html as components_html
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from outreach.view_helpers import (NA, creators_frame, describe_email_result, describe_run,  # noqa: E402
                                   engagement_pct, error_text, funnel_rows, has_message, message_notice,
                                   message_origin, outreach_counts, pick_default_creator, status_badge,
                                   status_label, copy_button_html)

from outreach import config, messaging  # noqa: E402
from outreach.llm_service import LLMError  # noqa: E402

load_dotenv()
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
START_API_HINT = "uvicorn outreach.api:create_app --factory"


def call(method: str, path: str, **kwargs):
    """Returns (data, error_detail). Never raises."""
    try:
        resp = requests.request(method, f"{API_URL}{path}", timeout=60, **kwargs)
    except requests.RequestException as exc:
        return None, f"Cannot reach the API at {API_URL} ({type(exc).__name__}). Start it with: {START_API_HINT}"
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if resp.status_code >= 400:
        return None, str(payload.get("detail") if isinstance(payload, dict) else resp.text[:200])
    return payload, None


def flash(kind: str, text: str, detail: str | None = None) -> None:
    """Show a message after the next rerun, with optional technical details tucked away."""
    st.session_state["flash"] = (kind, text, detail)
    st.rerun()


def generate_ai_message(cid: str, force: bool = False) -> None:
    """On-demand AI generation for the ONE selected creator. Only ever called from a button click."""
    if not config.BRAND_NAME or not config.SENDER_NAME:
        flash("error", "Message generation is not configured. Set BRAND_NAME and SENDER_NAME in the .env file.")
    sender = messaging.Sender(config.BRAND_NAME, config.SENDER_NAME, config.COLLAB_TYPE, config.BRAND_DESCRIPTION)
    try:
        with st.spinner("Generating a personalised message..."):
            result = messaging.generate_one(cid, config.DATA_DIR / "influencers_all.json",
                                            config.DATA_DIR / "messages.json", sender, messaging.GroqService,
                                            force=force)
    except messaging.OnDemandError as exc:
        flash("error", f"A message cannot be generated for this creator: {exc}.")
    except LLMError as exc:
        flash("error", "The AI service could not generate a message right now. Nothing was changed; "
                       "you can try again.", str(exc))
    outcome = result["outcome"]
    if outcome == "kept_existing":
        flash("warning", "The new AI output did not pass validation, so the existing valid message was kept.",
              result.get("error"))
    _, err = call("POST", "/import")      # existing JSON -> tracker workflow; outreach statuses are kept
    if err:
        flash("warning", "The message was saved, but the tracker could not be refreshed. "
                         "Use 'Import or refresh data' in the sidebar.", err)
    if outcome == "reused":
        flash("info", "A valid AI-generated message already exists for this creator, so no new AI request was made.")
    if outcome == "fallback":
        flash("warning", "The AI output did not pass validation after several attempts, so the standard template "
                         "was saved instead. It is not AI-generated.", result["message"].get("error"))
    flash("success", "AI message generated and saved for this creator.")


def generation_button(cid: str, label: str, force: bool, help_text: str) -> None:
    if st.button(label, key=f"gen_{'redo' if force else 'new'}_{cid}", help=help_text):
        generate_ai_message(cid, force)


def badge(text: str, color: str) -> None:
    if hasattr(st, "badge"):
        st.badge(text, color=color)
    else:
        st.markdown(f"**{text}**")


def copy_button(text: str, label: str) -> None:
    """One-click copy to clipboard (the code box above it also has its own copy icon)."""
    if text:
        components_html(copy_button_html(text, label), height=44)


FUNNEL_SPEC = {
    "height": 190, "width": "container",
    "encoding": {"y": {"field": "Stage", "type": "nominal", "sort": None, "axis": {"title": None}},
                 "x": {"field": "Creators", "type": "quantitative", "axis": {"title": None, "tickMinStep": 1}}},
    "layer": [
        {"mark": {"type": "bar", "cornerRadiusEnd": 4, "height": 22},
         "encoding": {"color": {"field": "Stage", "type": "nominal", "legend": None, "scale": {
             "domain": ["Discovered", "Qualified", "With email", "Message ready", "Emailed or simulated"],
             "range": ["#6B7A99", "#5B8DEF", "#2FA37A", "#E0A030", "#9B7BEA"]}},
             "tooltip": [{"field": "Stage"}, {"field": "Creators"}]}},
        {"mark": {"type": "text", "align": "left", "dx": 5, "fontWeight": "bold"},
         "encoding": {"text": {"field": "Creators", "type": "quantitative"}}},
    ],
}


def reset_filters() -> None:
    st.session_state.update(search="", f_qual="All", f_platform="All", f_msg=False, f_email="All", f_dm="All")


st.set_page_config(page_title="Creator outreach dashboard", layout="wide")
st.markdown("<style>.block-container{padding-top:2.2rem;max-width:1280px}</style>", unsafe_allow_html=True)
st.title("Creator outreach dashboard")
st.caption("Review qualified fashion and beauty creators, preview outreach messages and track every contact. "
           "Email actions are simulated unless real sending is explicitly enabled.")

if "flash" in st.session_state:
    kind, text, detail = st.session_state.pop("flash")
    getattr(st, kind)(text)
    if detail:
        with st.expander("Details"):
            st.code(detail, language=None, wrap_lines=True)

cfg, err = call("GET", "/config")
if err:
    st.error("Cannot reach the API. Make sure the outreach service is running, then refresh this page.")
    with st.expander("Details"):
        st.code(err, language=None, wrap_lines=True)
    st.stop()
real = cfg["real_sending"]
if real:
    st.error("REAL SENDING IS ENABLED: email actions send actual emails. "
             "Instagram DMs are still sent manually.")
else:
    st.info("SIMULATION MODE: no email leaves this machine. Email actions only record a simulated outcome, and "
            "Instagram DMs are always sent manually by you.")

# ---------------------------------------------------------------- sidebar
STATUS_VALUES = ["All", "not_contacted", "simulated", "sent", "failed", "skipped", "pending"]
with st.sidebar:
    st.header("Data")
    if st.button("Import or refresh data", help="Loads the latest creator and message files into the tracker. "
                                                "Existing outreach statuses are kept."):
        with st.spinner("Importing creators and messages..."):
            res, err = call("POST", "/import")
        if err:
            flash("error", "The import did not complete. Nothing was lost; you can try again.", err)
        flash("success", "Import finished. Outreach statuses were left unchanged.",
              None if res is None else str(res))

    st.header("Search and filters")
    search = st.text_input("Search", key="search", placeholder="Name, niche, theme or email")
    qualification = st.selectbox("Qualification", ["All", "PASS", "FAIL"], key="f_qual",
                                 format_func=lambda v: {"PASS": "Qualified", "FAIL": "Not qualified"}.get(v, v))
    unfiltered, _ = call("GET", "/influencers")
    platform = st.selectbox("Platform", ["All"] + sorted({r["platform"] for r in (unfiltered or [])}),
                            key="f_platform")
    only_msg = st.checkbox("Only creators with messages", key="f_msg")
    email_status = st.selectbox("Email status", STATUS_VALUES, key="f_email",
                                format_func=lambda v: "All" if v == "All" else status_label("email", v))
    dm_status = st.selectbox("DM status", ["All", "not_contacted", "sent", "failed", "skipped"], key="f_dm",
                             format_func=lambda v: "All" if v == "All" else status_label("instagram_dm", v))
    st.button("Reset filters", on_click=reset_filters)
    with st.expander("Technical details"):
        st.write(f"Backend: {API_URL}")
        st.json(cfg)

params = {k: v for k, v in (("qualification", qualification), ("platform", platform),
                            ("email_status", email_status), ("dm_status", dm_status)) if v != "All"}
with st.spinner("Loading creators..."):
    rows, err = (unfiltered, None) if not params else call("GET", "/influencers", params=params)
    stats, _ = call("GET", "/stats")
if err:
    st.error("Creators could not be loaded. Please refresh the page.")
    with st.expander("Details"):
        st.code(err, language=None, wrap_lines=True)
    st.stop()
rows = rows or []
total_rows = len(rows)
if search.strip():
    needle = search.strip().lower()
    rows = [r for r in rows if needle in " ".join(str(x) for x in (
        r.get("name"), r.get("niche"), r.get("contact_email"), r.get("profile_url"),
        ", ".join(r.get("content_themes") or [])) if x).lower()]
if only_msg:
    rows = [r for r in rows if has_message(r)]

# ---------------------------------------------------------------- KPIs
if stats:
    out = stats["outreach"]
    sim, sent = out.get("email:simulated", 0), out.get("email:sent", 0)
    dm_sent = out.get("instagram_dm:sent", 0)
    k = st.columns(4)
    for col, label, value, hint in (
            (k[0], "Total creators", stats["influencers"], "Discovered on YouTube"),
            (k[1], "Qualified", stats["qualified"], "Met all qualification criteria"),
            (k[2], "Qualified with email", stats["qualified_with_email"], "Have a public contact email"),
            (k[3], "Messages ready", stats["qualified_with_message"], "Qualified creators with a draft")):
        with col, st.container(border=True):
            st.metric(label, value, help=hint)
    counts = outreach_counts(stats)
    left, right = st.columns([3, 2])
    with left, st.container(border=True):
        st.markdown("**Creator funnel**")
        st.caption("From discovery to outreach. Not affected by the filters.")
        st.vega_lite_chart(pd.DataFrame(funnel_rows(stats), columns=["Stage", "Creators"]), FUNNEL_SPEC)
    with right, st.container(border=True):
        st.markdown("**Outreach progress**")
        contactable = max(stats["qualified_with_email"], 1)
        st.progress(min((sim + sent) / contactable, 1.0),
                    text=f"Emails: {sim + sent} of {stats['qualified_with_email']} contactable creators handled "
                         f"({sim} simulated, {sent} sent)")
        e, d = counts["email"], counts["dm"]
        st.markdown(f"Emails &nbsp; :blue-badge[{e['simulated']} simulated] :green-badge[{e['sent']} sent] "
                    f":red-badge[{e['failed']} failed] :gray-badge[{e['waiting']} waiting]")
        st.progress(min(dm_sent / max(stats["qualified_with_message"], 1), 1.0),
                    text=f"Instagram DMs: {dm_sent} of {stats['qualified_with_message']} ready messages marked "
                         "as sent manually")
        st.markdown(f"DMs &nbsp; :green-badge[{d['sent']} sent manually] :red-badge[{d['failed']} failed] "
                    f":gray-badge[{d['waiting']} waiting]")

if not rows:
    if total_rows == 0 and not params:
        st.info("No creators have been imported yet. Use 'Import or refresh data' in the sidebar to get started.")
    else:
        st.info("No creators match the current search and filters. Try 'Reset filters' in the sidebar.")
    st.stop()

review_tab, outreach_tab = st.tabs(["Review creators", "Run email outreach"])

# ---------------------------------------------------------------- review tab
with review_tab:
    st.caption(f"Showing {len(rows)} of {total_rows} creators")
    st.dataframe(creators_frame(rows), hide_index=True, width="stretch", height=min(38 + 35 * len(rows), 380),
                 column_config={
        "Profile": st.column_config.LinkColumn("Profile", display_text="Open channel"),
        "Followers": st.column_config.NumberColumn("Followers", format="%d"),
        "Engagement (%)": st.column_config.NumberColumn("Engagement (%)", format="%.2f"),
    })
    st.caption("Empty followers or engagement means the value is not available from the public API.")

    by_id = {r["channel_id"]: r for r in rows}
    if st.session_state.get("creator") not in by_id:
        st.session_state["creator"] = pick_default_creator(rows)
    st.subheader("Creator review")
    cid = st.selectbox("Choose a creator", list(by_id), key="creator", format_func=lambda c: (
        f"{by_id[c]['name']} - {'qualified' if by_id[c]['filter_status'] == 'PASS' else 'not qualified'}"
        f"{' - message ready' if has_message(by_id[c]) else ''}"))
    inf = by_id[cid]

    head, tag = st.columns([4, 1])
    head.markdown(f"### {inf['name']}")
    with tag:
        badge("Qualified" if inf["filter_status"] == "PASS" else "Not qualified",
              "green" if inf["filter_status"] == "PASS" else "red")
    m = st.columns(4)
    m[0].metric("Followers", f"{inf['follower_count']:,}" if inf["follower_count"] is not None else NA)
    eng = engagement_pct(inf)
    m[1].metric("Engagement", f"{eng}%" if eng is not None else NA, help="Likes and comments per view, recent videos")
    m[2].metric("Niche", inf.get("niche") or NA)
    m[3].metric("Platform", inf["platform"])
    links = [f"[YouTube channel]({inf['profile_url']})"]
    if inf.get("instagram_url"):
        links.append(f"[Instagram]({inf['instagram_url']})")
    if inf.get("website"):
        links.append(f"[Website]({inf['website']})")
    st.markdown(" | ".join(links))
    st.markdown(f"Email {status_badge('email', inf['email_status'])} &nbsp; "
                f"Instagram DM {status_badge('instagram_dm', inf['dm_status'])}")
    has_email = inf["contact_email"] not in ("", "Not Found")
    st.write(f"**Contact email:** {inf['contact_email'] if has_email else 'No public email found'}")
    st.write(f"**Content themes:** {', '.join(inf['content_themes']) or 'None identified'}")
    with st.expander("Why this creator did or did not qualify", expanded=inf["filter_status"] == "FAIL"):
        for reason in inf["filter_reasons"]:
            st.write("- " + reason)

    st.markdown("#### Message preview")
    msg = inf.get("message")
    if inf["filter_status"] != "PASS":
        st.info("This creator did not meet the qualification criteria, so no outreach messages are prepared.")
    elif not has_message(inf):
        st.info("No personalized messages are available for this creator yet.")
        st.caption("Email and Instagram status can be recorded once a message is available for this creator.")
        generation_button(cid, "Generate AI Message", False, "Creates an email and Instagram DM for this creator only.")
    else:
        origin_label, origin_colour = message_origin(msg)
        st.markdown(f":{origin_colour}-badge[{origin_label}]")
        if msg.get("source") == "llm":
            st.caption("AI-generated from verified creator information. Please review before use.")
            generation_button(cid, "Regenerate AI message", True,
                              "Makes a new AI request for this creator and replaces the current message if valid.")
        else:
            generation_button(cid, "Generate AI Message", False,
                              "Creates an email and Instagram DM for this creator only.")
        with st.expander("Message details"):
            st.write(f"Source: {origin_label}")
            st.write(f"Validation result: {msg.get('validation_status') or NA}")
            st.write(f"Generated at: {msg.get('generated_at') or NA}")
            if msg.get("error"):
                st.code(msg["error"], language=None, wrap_lines=True)
        email_col, dm_col = st.columns(2)
        with email_col, st.container(border=True):
            st.markdown("**Email**")
            st.caption("Subject")
            st.code(msg["subject"] or "(no subject)", language=None, wrap_lines=True)
            copy_button(msg["subject"] or "", "Copy subject")
            st.caption(f"Body ({msg['email_word_count'] or '?'} words)")
            st.code(msg["email"], language=None, wrap_lines=True)
            copy_button(msg["email"], "Copy email body")
            st.markdown(f"Status: {status_badge('email', inf['email_status'])}")
            if not has_email:
                st.caption("No public email was found, so email outreach is skipped for this creator.")
            confirm_one = st.checkbox("I confirm I want to send a real email", key=f"confirm_{cid}") if real else False
            done = inf["email_status"] in ("simulated", "sent", "pending")
            label = "Send this email (REAL)" if real else "Simulate sending this email"
            if st.button(label, key=f"send_{cid}", disabled=done or not has_email or (real and not confirm_one),
                         help="Records a simulated outcome only; nothing is sent." if not real else None):
                with st.spinner("Working..."):
                    res, err = call("POST", f"/outreach/email/{cid}", json={"confirm": confirm_one})
                if err:
                    flash("error", error_text(err), err)
                kind, text, detail = describe_email_result(res)
                flash(kind, text, detail)
            if done:
                st.caption("This email has already been recorded, so it cannot be repeated.")
        with dm_col, st.container(border=True):
            st.markdown("**Instagram DM**")
            st.caption(f"Message ({msg['dm_word_count'] or '?'} words)")
            st.code(msg["dm"], language=None, wrap_lines=True)
            copy_button(msg["dm"], "Copy DM")
            st.caption("This dashboard never sends Instagram messages. Copy the text, send it yourself in "
                       "Instagram, then record the outcome here.")
            st.markdown(f"Status: {status_badge('instagram_dm', inf['dm_status'])}")
            note = st.text_input("Note (optional)", key=f"note_{cid}")
            d1, d2 = st.columns(2)
            for col, status, text in ((d1, "sent", "Mark DM as sent manually"), (d2, "failed", "Mark DM as failed")):
                if col.button(text, key=f"dm_{status}_{cid}", disabled=inf["dm_status"] == "sent"):
                    res, err = call("POST", f"/outreach/dm/{cid}/status", json={"status": status, "note": note or None})
                    if err:
                        flash("error", error_text(err), err)
                    flash("success", "Recorded as sent manually. This only updates the tracker." if status == "sent"
                          else "Recorded as failed. You can try again later.")

    detail_data, _ = call("GET", f"/influencers/{cid}")
    history = (detail_data or {}).get("history") or []
    if history:
        with st.expander("Outreach history"):
            hist = pd.DataFrame(history).reindex(columns=["created_at", "channel", "mode", "status", "recipient",
                                                          "error", "note"]).astype("string")
            hist["status"] = [status_label(c, s) for c, s in zip(hist["channel"], hist["status"])]
            st.dataframe(hist.rename(columns=lambda c: c.replace("_", " ").capitalize()), hide_index=True,
                         width="stretch")

# ---------------------------------------------------------------- batch tab
with outreach_tab:
    st.subheader("Email outreach for all qualified creators")
    st.write("Processes qualified creators who have a usable message and a public email address. Creators that "
             "were already handled are skipped, so running this twice never duplicates anything.")
    c1, c2 = st.columns([1, 2])
    limit = c1.number_input("Maximum emails this run", min_value=1, max_value=500, value=10)
    confirm_batch = c2.checkbox("I confirm I want to send real emails", key="confirm_batch") if real else False
    label = "Send emails (REAL)" if real else "Run simulated email outreach"
    if st.button(label, disabled=real and not confirm_batch, type="primary"):
        with st.spinner("Processing creators..."):
            res, err = call("POST", "/outreach/email/run", json={"limit": int(limit), "confirm": confirm_batch})
        if err:
            flash("error", error_text(err), err)
        kind, text = describe_run(res)
        flash(kind, text)
