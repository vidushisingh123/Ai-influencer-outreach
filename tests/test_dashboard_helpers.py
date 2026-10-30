import pandas as pd

from outreach.view_helpers import (creators_frame, describe_email_result, describe_run, error_text, explain_error,
                                   message_notice, pick_default_creator, status_label)


def row(cid, status="PASS", message=True, **kw):
    r = dict(channel_id=cid, name=cid, platform="YouTube", profile_url=f"https://www.youtube.com/channel/{cid}",
             niche="Fashion & Beauty", follower_count=20000, engagement_rate=0.0452, filter_status=status,
             contact_email="Not Found", email_status="not_contacted", dm_status="not_contacted",
             message={"email": "e", "dm": "d", "subject": "s", "source": "llm"} if message else None)
    r.update(kw)
    return r


def test_default_creator_prefers_qualified_creator_with_a_message():
    rows = [row("A", message=False), row("B", status="FAIL"), row("C"), row("D")]
    assert pick_default_creator(rows) == "C"


def test_default_creator_fallbacks():
    assert pick_default_creator([row("A", status="FAIL", message=False), row("B", message=False)]) == "B"
    assert pick_default_creator([row("A", status="FAIL", message=False)]) == "A"
    assert pick_default_creator([row("A", status="FAIL"), row("B", message=False)]) == "A"
    assert pick_default_creator([]) is None


def test_creators_frame_has_consistent_numeric_types_with_missing_values():
    df = creators_frame([row("A"), row("B", follower_count=None, engagement_rate=None)])
    assert str(df["Followers"].dtype) == "Int64" and df["Engagement (%)"].dtype.kind == "f"
    assert df["Engagement (%)"].iloc[0] == 4.52 and pd.isna(df["Engagement (%)"].iloc[1])
    assert pd.isna(df["Followers"].iloc[1])
    assert creators_frame([]).shape[0] == 0
    assert list(df["Message"]) == ["Ready", "Ready"]
    assert list(creators_frame([row("A", message=False)])["Message"]) == ["Not yet"]


def test_status_labels_never_imply_delivery_for_simulation():
    assert status_label("email", "simulated") == "Simulated (not sent)"
    assert status_label("instagram_dm", "sent") == "Sent manually"
    assert status_label("email", None) == "Not contacted"


def test_simulated_results_never_claim_delivery():
    kind, text = describe_run({"mode": "simulation", "outcomes": {"simulated": 3, "skipped": 1}})
    assert kind == "success" and "3 email(s) recorded as simulated" in text and "No emails were sent" in text
    kind, text, _ = describe_email_result({"status": "simulated", "mode": "simulation", "error": None})
    assert kind == "success" and "no email was sent" in text
    assert describe_run({"mode": "simulation", "outcomes": {}})[0] == "info"
    kind, text = describe_run({"mode": "simulation", "outcomes": {"simulated": 1}, "aborted": "stopped"})
    assert kind == "warning" and "stopped" in text


def test_errors_are_friendly_and_do_not_expose_developer_commands():
    assert "already recorded" in explain_error("duplicate outreach: already contacted")
    assert "uvicorn" not in explain_error("Cannot reach the API. Start it with: uvicorn outreach.api")
    assert message_notice({"source": "llm"}) is None
    assert "standard template" in message_notice({"source": "fallback_template"})


def test_error_text_keeps_the_specific_api_reason_but_hides_connection_details():
    text = error_text("this DM is already marked as sent")
    assert "already recorded" in text and "this DM is already marked as sent" in text
    assert "uvicorn" not in error_text("Cannot reach the API at x. Start it with: uvicorn outreach.api")
    assert error_text(None) == explain_error(None)


# ---------------------------------------------------------------- final polish helpers
def test_message_origin_never_calls_a_template_ai_generated():
    from outreach.view_helpers import message_origin
    assert message_origin({"source": "llm"}) == ("AI-generated", "green")
    label, colour = message_origin({"source": "fallback_template"})
    assert label == "Standard template" and "AI" not in label and colour == "orange"
    assert message_origin({})[0] == "Source unknown"


def test_template_notice_explains_why_and_is_not_claimed_as_ai():
    low = message_notice({"source": "fallback_template", "validation_status": "fallback_insufficient_context"})
    bad = message_notice({"source": "fallback_template", "validation_status": "fallback_after_invalid_output"})
    assert "not enough verified information" in low and "did not pass" in bad
    assert "not an AI-personalised message" in low and "not an AI-personalised message" in bad


def test_status_badges_use_distinct_colours_and_honest_labels():
    from outreach.view_helpers import status_badge
    assert status_badge("email", "simulated") == ":blue-badge[Simulated (not sent)]"
    assert status_badge("instagram_dm", "sent") == ":green-badge[Sent manually]"
    assert status_badge("email", None) == ":gray-badge[Not contacted]"
    assert status_badge("email", "failed").startswith(":red-badge[")


def test_funnel_and_outreach_counts_follow_the_stats():
    from outreach.view_helpers import funnel_rows, outreach_counts
    stats = {"influencers": 391, "qualified": 53, "qualified_with_email": 24, "qualified_with_message": 2,
             "outreach": {"email:simulated": 3, "email:sent": 1, "email:failed": 1, "instagram_dm:sent": 1}}
    assert funnel_rows(stats) == [("Discovered", 391), ("Qualified", 53), ("With email", 24),
                                  ("Message ready", 2), ("Emailed or simulated", 4)]
    c = outreach_counts(stats)
    assert c["email"] == {"simulated": 3, "sent": 1, "failed": 1, "skipped": 0, "waiting": 19}
    assert c["dm"] == {"sent": 1, "failed": 0, "skipped": 0, "waiting": 1}
    empty = outreach_counts({"influencers": 0, "qualified": 0, "qualified_with_email": 0,
                             "qualified_with_message": 0, "outreach": {}})
    assert empty["email"]["waiting"] == 0 and empty["dm"]["waiting"] == 0


def test_copy_button_html_escapes_text_and_cannot_break_out_of_the_script():
    from outreach.view_helpers import copy_button_html
    out = copy_button_html('Hi "Ami"</script><b>x</b>\nline 2', "Copy <body>")
    assert "</script><b>" not in out and "Copy &lt;body&gt;" in out
    assert out.count("</script>") == 1 and "navigator.clipboard.writeText" in out
