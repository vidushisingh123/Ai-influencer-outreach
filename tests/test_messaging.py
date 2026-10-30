import json

import pytest

from outreach import config
from outreach.messaging import main

from outreach.llm_service import LLMFatalError, LLMResponseError, LLMTransientError
from outreach.messaging import (EMAIL_WORDS, DM_WORDS, Sender, build_context, build_fallback, count_words,
                                generate_for, parse_response, run, validate_messages)
from outreach.models import Influencer, RecentVideo
from datetime import datetime, timezone

SENDER = Sender(brand="GlowLab", sender_name="Riya", collab_type="UGC content creation",
                brand_description="GlowLab makes fragrance-free skincare.")
TITLE = "My nightly skincare routine"


def words(n, first="Hi"):
    return " ".join([first] + ["word"] * (n - 1))


def reply(email=75, dm=22, subject="Collab idea for you"):
    return json.dumps({"subject": subject, "email": words(email), "dm": words(dm)})


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


def record(cid="UC1", name="Ami Glow", status="PASS", themes=("Skincare",), titles=(TITLE,), email="ami@glow.test"):
    inf = Influencer(
        channel_id=cid, name=name, profile_url=f"https://www.youtube.com/channel/{cid}", follower_count=20000,
        engagement_rate=0.05, niche="Fashion & Beauty", content_themes=list(themes), contact_email=email,
        recent_videos=[RecentVideo(video_id=f"{cid}{i}", title=t, url="https://www.youtube.com/watch?v=x",
                                   published_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
                       for i, t in enumerate(titles)])
    d = inf.model_dump(mode="json")      # same schema Phase 1 writes to influencers_all.json
    d["filter_status"] = status
    return d


# ---------------------------------------------------------------- validation + parsing
def test_validation_limits_and_quotes():
    assert validate_messages("Hi there", words(75), words(22), [TITLE]) == []
    assert any("email has 95" in p for p in validate_messages("s", words(95), words(22), []))
    assert any("DM has 10" in p for p in validate_messages("s", words(75), words(10), []))
    assert any("placeholder" in p for p in validate_messages("s", words(75) + " [Brand]", words(22), []))
    assert any("not a provided video title" in p
               for p in validate_messages("s", words(70) + ' loved "your secret spa day" video', words(22), [TITLE]))
    assert validate_messages("s", words(70) + f' about "{TITLE}" today', words(22), [TITLE]) == []


def test_parse_response_variants():
    assert parse_response('```json\n{"subject":"a","email":"b","dm":"c"}\n```') == {"subject": "a", "email": "b", "dm": "c"}
    for bad in ("not json", '{"subject":"a","email":"b"}', '{"subject":"a","email":"","dm":"c"}', "[1,2]"):
        with pytest.raises(ValueError):
            parse_response(bad)


# ---------------------------------------------------------------- generate_for
def test_valid_first_attempt_and_prompt_contains_only_verified_context():
    svc = FakeService(reply())
    res = generate_for(record(), SENDER, svc)
    assert res["validation_status"] == "valid" and res["source"] == "llm" and res["attempts"] == 1
    assert (res["email_word_count"], res["dm_word_count"]) == (75, 22) and res["error"] is None
    assert TITLE in svc.prompts[0] and "Skincare" in svc.prompts[0]
    assert "ami@glow.test" not in svc.prompts[0]          # email address is never sent to the model


def test_length_violation_triggers_retry_with_feedback():
    svc = FakeService(reply(email=120), reply())
    res = generate_for(record(), SENDER, svc)
    assert res["validation_status"] == "valid" and res["attempts"] == 2
    assert "email has 120 words" in svc.prompts[1]


def test_attempts_are_bounded_then_fallback_is_used():
    svc = FakeService(reply(email=10), reply(dm=100), reply(email=200))
    res = generate_for(record(), SENDER, svc)
    assert len(svc.prompts) == 3
    assert res["validation_status"] == "fallback_after_invalid_output" and res["source"] == "fallback_template"
    assert res["error"] and EMAIL_WORDS[0] <= res["email_word_count"] <= EMAIL_WORDS[1]
    assert DM_WORDS[0] <= res["dm_word_count"] <= DM_WORDS[1]


def test_malformed_and_empty_outputs_are_handled():
    svc = FakeService("sorry, I can't", LLMResponseError("empty model response"), '{"subject": "x"}')
    res = generate_for(record(), SENDER, svc)
    assert res["validation_status"] == "fallback_after_invalid_output" and "malformed" in res["error"]
    assert len(svc.prompts) == 3


def test_insufficient_context_skips_llm():
    svc = FakeService()
    res = generate_for(record(themes=(), titles=()), SENDER, svc)
    assert svc.prompts == [] and res["validation_status"] == "fallback_insufficient_context"
    assert "Ami Glow" in res["email"] and "insufficient" in res["error"]
    assert 60 <= res["email_word_count"] <= 90 and 15 <= res["dm_word_count"] <= 30


@pytest.mark.parametrize("name", ["Ami", "Ami Glow Beauty Studio Official", "The Very Long Channel Name Of Style"])
def test_fallback_stays_within_limits(name):
    ctx = build_context(record(name=name, themes=("Skincare", "Makeup", "Nails")))
    _, email, dm = build_fallback(ctx, SENDER)
    assert EMAIL_WORDS[0] <= count_words(email) <= EMAIL_WORDS[1]
    assert DM_WORDS[0] <= count_words(dm) <= DM_WORDS[1]


# ---------------------------------------------------------------- batch runner
def write_input(tmp_path, records):
    p = tmp_path / "influencers_all.json"
    p.write_text(json.dumps(records))
    return p


def load(path):
    return json.loads(path.read_text())["messages"]


def test_run_only_processes_qualified_and_saves_schema(tmp_path):
    src = write_input(tmp_path, [record("A"), record("B", name="Failed Fay", status="FAIL"), record("C", name="Nora")])
    out = tmp_path / "messages.json"
    summary = run(src, out, SENDER, FakeService(reply(), reply()))
    msgs = load(out)
    assert [m["channel_id"] for m in msgs] == ["A", "C"]            # FAIL influencer gets nothing
    assert summary["skipped_failed"] == 1 and summary["by_status"] == {"valid": 2}
    assert {"channel_id", "email", "dm", "validation_status", "error"} <= set(msgs[0])


def test_run_resumes_and_force_regenerates(tmp_path):
    src, out = write_input(tmp_path, [record("A"), record("C", name="Nora")]), tmp_path / "messages.json"
    run(src, out, SENDER, FakeService(reply(), reply()))
    idle = FakeService()
    s = run(src, out, SENDER, idle)
    assert idle.prompts == [] and s["reused_existing"] == 2
    forced = FakeService(reply(), reply())
    run(src, out, SENDER, forced, force=True)
    assert len(forced.prompts) == 2


def test_transient_failure_is_recorded_and_run_continues(tmp_path):
    src, out = write_input(tmp_path, [record("A"), record("C", name="Nora")]), tmp_path / "messages.json"
    run(src, out, SENDER, FakeService(LLMTransientError("rate limited"), reply()))
    msgs = {m["channel_id"]: m for m in load(out)}
    assert msgs["A"]["validation_status"] == "error" and msgs["A"]["email"] is None and "rate limited" in msgs["A"]["error"]
    assert msgs["C"]["validation_status"] == "valid"
    again = FakeService(reply())                                    # errors are retried on the next run
    run(src, out, SENDER, again)
    assert len(again.prompts) == 1 and {m["validation_status"] for m in load(out)} == {"valid"}


def test_fatal_error_stops_run_but_keeps_progress(tmp_path):
    src, out = write_input(tmp_path, [record("A"), record("B", name="Bea"), record("C", name="Nora")]), tmp_path / "m.json"
    s = run(src, out, SENDER, FakeService(reply(), LLMFatalError("AuthenticationError")))
    assert "fatal" in s["aborted"] and [m["channel_id"] for m in load(out)] == ["A"]


def test_consecutive_failures_trip_circuit_breaker(tmp_path):
    recs = [record(f"U{i}", name=f"Name{i}") for i in range(6)]
    svc = FakeService(*[LLMTransientError("down")] * 6)
    s = run(write_input(tmp_path, recs), tmp_path / "m.json", SENDER, svc, max_consecutive_errors=3)
    assert "consecutive" in s["aborted"] and len(svc.prompts) == 3


# ---------------------------------------------------------------- CLI configuration errors
def test_cli_missing_api_key_exits_cleanly_without_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "BRAND_NAME", "GlowLab")
    monkeypatch.setattr(config, "SENDER_NAME", "Riya")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    src = write_input(tmp_path, [record("A")])
    assert main(["--input", str(src), "--output", str(tmp_path / "m.json")]) == 2
    out = capsys.readouterr().out
    assert "GROQ_API_KEY is not set" in out and not (tmp_path / "m.json").exists()


def test_cli_requires_brand_and_sender(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "BRAND_NAME", "")
    assert main(["--input", str(write_input(tmp_path, [record("A")]))]) == 2
    assert "BRAND_NAME and SENDER_NAME" in capsys.readouterr().out
