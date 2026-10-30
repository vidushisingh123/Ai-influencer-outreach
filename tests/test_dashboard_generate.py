"""Dashboard: 'Generate AI Message' for the selected creator. Fake API + mocked Groq; no server, no network."""
import json
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from outreach import config, messaging
from outreach.llm_service import LLMTransientError

SCRIPT = str(Path(__file__).parent.parent / "outreach" / "dashboard.py")
TITLE = "My nightly skincare routine"
NAMES = {"UC_A": "Ami Glow", "UC_B": "Bea Bloom", "UC_C": "Cara Chic"}


def words(n, first="Hi"):
    return " ".join([first] + ["word"] * (n - 1))


GENERATED = {"subject": "Collab idea for you", "email": words(75, "Dear"), "dm": words(22, "Hey")}


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


def stored(cid, name, tag):
    return {"channel_id": cid, "name": name, "subject": f"Subject {tag}", "email": words(70, tag), "dm": words(20, tag),
            "email_word_count": 70, "dm_word_count": 20, "source": "llm", "validation_status": "valid",
            "attempts": 1, "model": "m", "error": None, "generated_at": "2026-10-01T00:00:00+00:00"}


class Env:
    """messages.json on disk + a fake 'tracker' that only learns about it when POST /import is called."""

    def __init__(self, tmp_path):
        self.dir, self.api_calls, self.tracker = tmp_path, [], {}
        recs = [{"channel_id": c, "name": n, "platform": "YouTube", "niche": "Fashion & Beauty",
                 "content_themes": ["Skincare"], "recent_videos": [{"title": TITLE}], "contact_email": "a@glow.test",
                 "filter_status": "PASS"} for c, n in NAMES.items()]
        (tmp_path / "influencers_all.json").write_text(json.dumps(recs), encoding="utf-8")
        self.messages = {"UC_B": stored("UC_B", "Bea Bloom", "B")}
        self.write()
        self.tracker = {"UC_B": self.messages["UC_B"]}      # UC_B is imported; UC_C will be file-only

    def write(self):
        (self.dir / "messages.json").write_text(json.dumps({"messages": list(self.messages.values())}), encoding="utf-8")

    def on_disk(self):
        return {m["channel_id"]: m for m in json.loads((self.dir / "messages.json").read_text(encoding="utf-8"))["messages"]}

    def sync(self):
        self.tracker = dict(self.on_disk())

    def row(self, cid):
        m = self.tracker.get(cid)
        msg = None if m is None else {k: m.get(k) for k in ("subject", "email", "dm", "email_word_count", "dm_word_count",
                                                           "source", "validation_status", "error", "generated_at")}
        return {"channel_id": cid, "name": NAMES[cid], "platform": "YouTube",
                "profile_url": f"https://www.youtube.com/channel/{cid}", "niche": "Fashion & Beauty",
                "follower_count": 12000, "engagement_rate": 0.045, "content_themes": ["Skincare"],
                "contact_email": "a@glow.test", "email_source": "public_channel_description", "instagram_url": None,
                "filter_status": "PASS", "filter_reasons": ["PASS followers: 12,000"], "message": msg,
                "email_status": "not_contacted", "dm_status": "not_contacted"}

    def fake_request(self, method, url, **kw):
        path = url.split("8000", 1)[-1]
        self.api_calls.append((method, path))
        if path == "/config":
            return Resp({"email_mode": "simulation", "email_mode_label": "SIMULATED", "real_sending": False})
        if path == "/influencers":
            return Resp([self.row(c) for c in NAMES])
        if path.startswith("/influencers/"):
            return Resp({**self.row(path.rsplit("/", 1)[-1]), "history": []})
        if path == "/stats":
            return Resp({"influencers": 3, "qualified": 3, "qualified_with_email": 3,
                         "qualified_with_message": len(self.tracker), "outreach": {}})
        if (method, path) == ("POST", "/import"):
            self.sync()
            return Resp({"ok": True})
        return Resp({"detail": "not found"}, 404)


class FakeGroq:
    """Stands in for GroqService: records prompts, never touches the network."""
    script, prompts, created = [], [], 0
    model = "fake-model"

    def __init__(self, *a, **k):
        type(self).created += 1

    def complete(self, system, user, max_tokens=600):
        type(self).prompts.append(user)
        item = type(self).script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    FakeGroq.script, FakeGroq.prompts, FakeGroq.created = [], [], 0
    monkeypatch.setattr(requests, "request", e.fake_request)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "BRAND_NAME", "GlowLab")
    monkeypatch.setattr(config, "SENDER_NAME", "Riya")
    monkeypatch.setattr(messaging, "GroqService", FakeGroq)
    return e


def app_for(cid):
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    next(s for s in at.selectbox if s.key == "creator").select(cid).run()
    return at


def button(at, label):
    return next(b for b in at.button if b.label == label)


def test_viewing_creators_never_calls_the_ai(env):
    at = app_for("UC_A")
    assert not at.exception and FakeGroq.created == 0 and FakeGroq.prompts == []
    assert any(b.label == "Generate AI Message" for b in at.button)       # creator without a message
    at = app_for("UC_B")
    assert not any(b.label == "Generate AI Message" for b in at.button)    # valid AI message: only explicit regenerate
    assert any(b.label == "Regenerate AI message" for b in at.button) and FakeGroq.prompts == []


def test_generate_for_selected_creator_saves_and_displays_it(env):
    FakeGroq.script = [json.dumps(GENERATED)]
    before_b = dict(env.on_disk()["UC_B"])
    at = app_for("UC_A")
    button(at, "Generate AI Message").click().run()
    assert not at.exception
    assert len(FakeGroq.prompts) == 1 and "Ami Glow" in FakeGroq.prompts[0]
    assert "Bea Bloom" not in FakeGroq.prompts[0] and "Cara Chic" not in FakeGroq.prompts[0]
    disk = env.on_disk()
    assert disk["UC_A"]["email"] == GENERATED["email"] and disk["UC_A"]["dm"] == GENERATED["dm"]
    assert disk["UC_A"]["source"] == "llm" and "UC_C" not in disk and disk["UC_B"] == before_b
    assert ("POST", "/import") in env.api_calls
    assert any("generated and saved" in s.value for s in at.success)
    codes = [c.value for c in at.code]
    assert GENERATED["email"] in codes and GENERATED["dm"] in codes and GENERATED["subject"] in codes
    assert any("AI-generated" in m.value for m in at.markdown)


def test_existing_valid_message_in_file_is_reused_without_an_api_call(env):
    env.messages["UC_C"] = stored("UC_C", "Cara Chic", "C")      # on disk but not yet imported into the tracker
    env.write()
    at = app_for("UC_C")
    button(at, "Generate AI Message").click().run()
    assert not at.exception and FakeGroq.created == 0 and FakeGroq.prompts == []
    assert any("no new AI request was made" in i.value for i in at.info)
    assert env.on_disk()["UC_C"]["email"] == words(70, "C")
    assert any(words(70, "C") == c.value for c in at.code)       # now visible after the tracker refresh


def test_regenerate_is_explicit_and_only_touches_that_creator(env):
    FakeGroq.script = [json.dumps(GENERATED)]
    at = app_for("UC_B")
    assert FakeGroq.prompts == []
    button(at, "Regenerate AI message").click().run()
    assert len(FakeGroq.prompts) == 1 and "Bea Bloom" in FakeGroq.prompts[0]
    assert env.on_disk()["UC_B"]["email"] == GENERATED["email"]


def test_generation_failure_is_handled_and_nothing_is_saved(env):
    FakeGroq.script = [LLMTransientError("rate limited")]
    at = app_for("UC_A")
    button(at, "Generate AI Message").click().run()
    assert not at.exception
    assert any("could not generate a message right now" in e.value for e in at.error)
    assert "UC_A" not in env.on_disk() and ("POST", "/import") not in env.api_calls


def test_invalid_ai_output_falls_back_to_a_template_not_labelled_ai(env):
    FakeGroq.script = [json.dumps({**GENERATED, "email": words(5)})] * 3
    at = app_for("UC_A")
    button(at, "Generate AI Message").click().run()
    assert not at.exception and len(FakeGroq.prompts) == 3
    assert env.on_disk()["UC_A"]["source"] == "fallback_template"
    assert any("not AI-generated" in w.value for w in at.warning)
    assert any("Standard template" in m.value for m in at.markdown)
    assert not any("AI-generated" in m.value for m in at.markdown)


def test_missing_brand_configuration_is_reported_without_an_ai_call(env, monkeypatch):
    monkeypatch.setattr(config, "BRAND_NAME", "")
    at = app_for("UC_A")
    button(at, "Generate AI Message").click().run()
    assert any("BRAND_NAME and SENDER_NAME" in e.value for e in at.error) and FakeGroq.created == 0
