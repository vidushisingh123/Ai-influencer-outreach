"""generate_one(): on-demand generation for ONE creator. Mocked service only; no Groq, no network."""
import json

import pytest

from outreach.llm_service import LLMTransientError
from outreach.messaging import DM_WORDS, EMAIL_WORDS, OnDemandError, Sender, generate_one

SENDER = Sender(brand="GlowLab", sender_name="Riya", brand_description="GlowLab makes fragrance-free skincare.")
TITLE = "My nightly skincare routine"


def words(n, first="Hi"):
    return " ".join([first] + ["word"] * (n - 1))


def reply(email=75, dm=22):
    return json.dumps({"subject": "Collab idea for you", "email": words(email), "dm": words(dm)})


class FakeService:
    model = "fake-model"

    def __init__(self, *script):
        self.script, self.prompts = list(script), []

    def complete(self, system, user, max_tokens=600):
        self.prompts.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Factory:
    """Counts how often a service (i.e. an API client) is created."""

    def __init__(self, *script):
        self.service, self.created = FakeService(*script), 0

    def __call__(self):
        self.created += 1
        return self.service


def rec(cid, name, status="PASS"):
    return {"channel_id": cid, "name": name, "platform": "YouTube", "niche": "Fashion & Beauty",
            "content_themes": ["Skincare"], "recent_videos": [{"title": TITLE}],
            "contact_email": "x@y.test", "filter_status": status}


def good(cid, name, tag="OLD"):
    return {"channel_id": cid, "name": name, "subject": f"Subject {tag}", "email": words(70), "dm": words(20),
            "email_word_count": 70, "dm_word_count": 20, "source": "llm", "validation_status": "valid",
            "attempts": 1, "model": "m", "error": None, "generated_at": "2026-10-01T00:00:00+00:00"}


def setup(tmp_path, existing=()):
    src = tmp_path / "influencers_all.json"
    src.write_text(json.dumps([rec("A", "Ami Glow"), rec("B", "Bea Bloom"), rec("C", "Cara Chic"),
                               rec("F", "Failed Fay", status="FAIL")]), encoding="utf-8")
    out = tmp_path / "messages.json"
    if existing:
        out.write_text(json.dumps({"messages": list(existing)}), encoding="utf-8")
    return src, out


def saved(out):
    return {m["channel_id"]: m for m in json.loads(out.read_text(encoding="utf-8"))["messages"]}


def test_generates_for_the_selected_creator_only(tmp_path):
    src, out = setup(tmp_path)
    f = Factory(reply())
    res = generate_one("B", src, out, SENDER, f)
    assert res["outcome"] == "generated" and f.created == 1 and len(f.service.prompts) == 1
    assert "Bea Bloom" in f.service.prompts[0] and "Ami Glow" not in f.service.prompts[0]
    assert set(saved(out)) == {"B"}
    m = saved(out)["B"]
    assert m["validation_status"] == "valid" and m["source"] == "llm" and m["name"] == "Bea Bloom"
    assert EMAIL_WORDS[0] <= m["email_word_count"] <= EMAIL_WORDS[1]
    assert DM_WORDS[0] <= m["dm_word_count"] <= DM_WORDS[1]


def test_other_creators_messages_are_not_modified(tmp_path):
    other = good("A", "Ami Glow", "KEEP")
    src, out = setup(tmp_path, [other])
    generate_one("C", src, out, SENDER, Factory(reply()))
    msgs = saved(out)
    assert msgs["A"] == other and msgs["C"]["validation_status"] == "valid"


def test_existing_valid_message_is_reused_without_any_api_client(tmp_path):
    existing = good("A", "Ami Glow")
    src, out = setup(tmp_path, [existing])
    f = Factory()
    res = generate_one("A", src, out, SENDER, f)
    assert res["outcome"] == "reused" and res["message"] == existing
    assert f.created == 0 and f.service.prompts == [] and saved(out)["A"] == existing


def test_force_regenerates_and_replaces_only_that_creator(tmp_path):
    a, b = good("A", "Ami Glow"), good("B", "Bea Bloom", "B-KEEP")
    src, out = setup(tmp_path, [a, b])
    f = Factory(reply(email=80))
    res = generate_one("A", src, out, SENDER, f, force=True)
    assert res["outcome"] == "generated" and len(f.service.prompts) == 1
    assert saved(out)["A"]["email_word_count"] == 80 and saved(out)["B"] == b


def test_failed_regeneration_never_replaces_a_valid_message(tmp_path):
    existing = good("A", "Ami Glow")
    src, out = setup(tmp_path, [existing])
    f = Factory(reply(email=5), reply(email=5), reply(email=5))
    res = generate_one("A", src, out, SENDER, f, force=True)
    assert res["outcome"] == "kept_existing" and res["error"] and saved(out)["A"] == existing


def test_invalid_output_saves_a_template_that_is_not_labelled_ai(tmp_path):
    src, out = setup(tmp_path)
    res = generate_one("A", src, out, SENDER, Factory(reply(email=5), reply(dm=99), reply(email=300)))
    m = saved(out)["A"]
    assert res["outcome"] == "fallback"
    assert m["source"] == "fallback_template" and m["validation_status"] == "fallback_after_invalid_output"
    assert EMAIL_WORDS[0] <= m["email_word_count"] <= EMAIL_WORDS[1]
    assert DM_WORDS[0] <= m["dm_word_count"] <= DM_WORDS[1]


def test_existing_template_is_replaced_by_a_valid_ai_message(tmp_path):
    tmpl = {**good("A", "Ami Glow"), "source": "fallback_template", "validation_status": "fallback_after_invalid_output"}
    src, out = setup(tmp_path, [tmpl])
    res = generate_one("A", src, out, SENDER, Factory(reply()))
    assert res["outcome"] == "generated" and saved(out)["A"]["source"] == "llm"


def test_api_failure_propagates_and_saves_nothing(tmp_path):
    src, out = setup(tmp_path)
    with pytest.raises(LLMTransientError):
        generate_one("A", src, out, SENDER, Factory(LLMTransientError("rate limited")))
    assert not out.exists()


def test_unknown_or_unqualified_creator_never_creates_a_client(tmp_path):
    src, out = setup(tmp_path)
    f = Factory()
    for cid in ("NOPE", "F"):
        with pytest.raises(OnDemandError):
            generate_one(cid, src, out, SENDER, f)
    with pytest.raises(OnDemandError):
        generate_one("A", tmp_path / "missing.json", out, SENDER, f)
    assert f.created == 0 and not out.exists()
