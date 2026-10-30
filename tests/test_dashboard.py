"""Smoke-tests the Streamlit script offline with a fake API (no server, no network)."""
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

SCRIPT = str(Path(__file__).parent.parent / "outreach" / "dashboard.py")


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


def influencer(cid, name, status, **kw):
    msg = {"subject": f"Collab idea for {name}", "email": "Hi there, email body", "dm": "Hi there, DM body",
           "email_word_count": 75, "dm_word_count": 22, "source": "llm", "validation_status": "valid",
           "error": None} if status == "PASS" else None
    base = {"channel_id": cid, "name": name, "platform": "YouTube", "profile_url": f"https://www.youtube.com/channel/{cid}",
            "niche": "Fashion & Beauty", "follower_count": 12000, "engagement_rate": 0.045, "content_themes": ["Skincare"],
            "contact_email": "a@glow.test", "email_source": "public_channel_description", "instagram_url": None,
            "filter_status": status, "filter_reasons": [f"{status} followers: 12,000"], "message": msg,
            "email_status": "not_contacted", "dm_status": "not_contacted"}
    base.update(kw)
    return base


@pytest.fixture
def fake_api(monkeypatch):
    calls = []
    rows = [influencer("UC_A", "Ami Glow", "PASS"), influencer("UC_D", "Failed Fay", "FAIL", follower_count=None)]

    def fake(method, url, **kw):
        path = url.split("8000", 1)[-1]
        calls.append((method, path, kw.get("json")))
        if path == "/config":
            return Resp({"email_mode": "simulation", "email_mode_label": "SIMULATED", "real_sending": False})
        if path == "/influencers":
            return Resp(rows)
        if path.startswith("/influencers/"):
            return Resp({**rows[0], "history": []})
        if path == "/stats":
            return Resp({"influencers": 2, "qualified": 1, "qualified_with_email": 1, "qualified_with_message": 1,
                         "outreach": {}})
        if path.startswith("/outreach/email/"):
            return Resp({"channel_id": "UC_A", "status": "simulated", "mode": "simulation", "recipient": "a@glow.test",
                         "error": None})
        if path.startswith("/outreach/dm/"):
            return Resp({"detail": "this DM is already marked as sent"}, 409)
        return Resp({"detail": "not found"}, 404)
    monkeypatch.setattr(requests, "request", fake)
    return calls


def test_dashboard_renders_in_simulation_mode(fake_api):
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert not at.exception
    assert any("SIMULATION MODE" in i.value for i in at.info)
    assert len(at.dataframe) >= 1 and any("Ami Glow" in m.value for m in at.markdown)


def test_simulate_button_calls_api_and_shows_outcome(fake_api):
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    next(b for b in at.button if b.label == "Simulate sending this email").click().run()
    assert not at.exception
    assert ("POST", "/outreach/email/UC_A", {"confirm": False}) in fake_api
    assert any("simulated" in s.value for s in at.success)


def test_api_error_message_is_shown(fake_api):
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    next(b for b in at.button if b.label == "Mark DM as sent manually").click().run()
    assert any("already marked as sent" in e.value for e in at.error)


def test_unreachable_api_shows_helpful_error(monkeypatch):
    def down(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "request", down)
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert not at.exception and any("Cannot reach the API" in e.value for e in at.error)


def test_search_box_filters_rows(fake_api):
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    # The empty state was deliberately reworded and changed from a warning to a friendly info message.
    no_match = "No creators match the current search and filters"
    assert len(at.dataframe) >= 1 and not any(no_match in i.value for i in at.info)
    next(t for t in at.text_input if t.key == "search").set_value("zzz-nobody").run()
    assert not at.exception and any(no_match in i.value for i in at.info)
    assert len(at.dataframe) == 0                      # no creators table for a search with no matches
    next(t for t in at.text_input if t.key == "search").set_value("ami").run()
    assert not at.exception and len(at.dataframe) >= 1  # search narrows to the matching creator


def test_real_mode_buttons_are_locked_until_confirmed(fake_api, monkeypatch):
    fake = requests.request

    def real_cfg(method, url, **kw):
        if url.endswith("/config"):
            return Resp({"email_mode": "smtp", "email_mode_label": "REAL", "real_sending": True})
        return fake(method, url, **kw)
    monkeypatch.setattr(requests, "request", real_cfg)
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert any("REAL SENDING" in e.value for e in at.error)
    assert next(b for b in at.button if b.label == "Send emails (REAL)").disabled
    assert next(b for b in at.button if b.label == "Send this email (REAL)").disabled
    next(c for c in at.checkbox if c.key == "confirm_UC_A").check().run()
    assert not next(b for b in at.button if b.label == "Send this email (REAL)").disabled


# ---------------------------------------------------------------- final polish
def test_creator_without_message_shows_friendly_empty_state(fake_api, monkeypatch):
    base = requests.request

    def no_message(method, url, **kw):
        if url.endswith("/influencers"):
            return Resp([influencer("UC_N", "Nina NoMessage", "PASS", message=None)])
        return base(method, url, **kw)
    monkeypatch.setattr(requests, "request", no_message)
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert not at.exception
    assert any("No personalized messages are available for this creator yet" in i.value for i in at.info)
    assert not any("python -m" in i.value for i in at.info)            # no developer instructions
    assert not any(b.label in ("Simulate sending this email", "Mark DM as sent manually") for b in at.button)


def test_template_messages_are_never_labelled_ai_generated(fake_api, monkeypatch):
    base = requests.request
    template = {"subject": "Collaboration idea", "email": "Hi Ami Glow, template body", "dm": "Hi Ami Glow, DM",
                "email_word_count": 75, "dm_word_count": 22, "source": "fallback_template",
                "validation_status": "fallback_after_invalid_output", "error": "malformed model output"}

    def fallback(method, url, **kw):
        if url.endswith("/influencers"):
            return Resp([influencer("UC_A", "Ami Glow", "PASS", message=template)])
        return base(method, url, **kw)
    monkeypatch.setattr(requests, "request", fallback)
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert not at.exception
    assert any("Standard template" in m.value for m in at.markdown)
    assert not any("AI-generated" in m.value for m in at.markdown)
    assert not any("not an AI-personalised message" in w.value for w in at.warning)


def test_ai_messages_are_labelled_and_previews_are_separate(fake_api):
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert not at.exception
    assert any("AI-generated" in m.value for m in at.markdown)
    codes = [c.value for c in at.code]
    assert "Collab idea for Ami Glow" in codes and "Hi there, email body" in codes and "Hi there, DM body" in codes
