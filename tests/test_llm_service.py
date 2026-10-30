from types import SimpleNamespace

import groq
import httpx
import pytest

from outreach.llm_service import (DEFAULT_MODEL, GroqService, LLMFatalError, LLMResponseError,
                                  LLMTransientError, MissingAPIKeyError)


def api_error(cls, status, headers=None):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return cls("boom", response=httpx.Response(status, request=req, headers=headers or {}), body=None)


def text_resp(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class FakeClient:
    """Mimics groq.Groq: client.chat.completions.create(...)."""

    def __init__(self, *script):
        self.script, self.calls, self.kwargs = list(script), 0, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def service(client, sleeps=None, **kw):
    return GroqService(client=client, sleep=(sleeps.append if sleeps is not None else lambda s: None), **kw)


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError) as exc:
        GroqService()
    assert "GROQ_API_KEY" in str(exc.value)


def test_model_comes_from_env_with_documented_default(monkeypatch):
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    assert GroqService(client=FakeClient()).model == DEFAULT_MODEL == "llama-3.3-70b-versatile"
    monkeypatch.setenv("GROQ_MODEL", "some-other-model")
    assert GroqService(client=FakeClient()).model == "some-other-model"


def test_success_sends_system_and_user_messages():
    client = FakeClient(text_resp("  hello  "))
    assert service(client, model="m1").complete("SYS", "USER") == "hello"
    sent = client.kwargs[0]
    assert sent["model"] == "m1"
    assert sent["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]


def test_rate_limit_then_success_honours_retry_after():
    sleeps = []
    client = FakeClient(api_error(groq.RateLimitError, 429, {"retry-after": "7"}), text_resp("ok"))
    assert service(client, sleeps).complete("s", "u") == "ok"
    assert client.calls == 2 and sleeps == [7.0]


def test_long_retry_after_fails_fast_without_waiting():
    sleeps = []
    client = FakeClient(api_error(groq.RateLimitError, 429, {"retry-after": "3600"}))
    with pytest.raises(LLMTransientError) as exc:
        service(client, sleeps).complete("s", "u")
    assert client.calls == 1 and sleeps == [] and "try again later" in str(exc.value)


def test_server_errors_use_backoff_and_are_bounded():
    sleeps = []
    client = FakeClient(*[api_error(groq.InternalServerError, 503) for _ in range(4)])
    with pytest.raises(LLMTransientError):
        service(client, sleeps, max_retries=3).complete("s", "u")
    assert client.calls == 4 and sleeps == [2.0, 4.0, 8.0]


def test_connection_errors_are_retried():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    client = FakeClient(groq.APIConnectionError(request=req), text_resp("ok"))
    assert service(client).complete("s", "u") == "ok"


def test_auth_error_is_fatal_and_not_retried():
    client = FakeClient(api_error(groq.AuthenticationError, 401))
    with pytest.raises(LLMFatalError) as exc:
        service(client).complete("s", "u")
    assert client.calls == 1 and "GROQ_API_KEY" in str(exc.value)


def test_unknown_model_is_fatal_with_hint():
    client = FakeClient(api_error(groq.NotFoundError, 404))
    with pytest.raises(LLMFatalError) as exc:
        service(client, model="no-such-model").complete("s", "u")
    assert "GROQ_MODEL" in str(exc.value) and client.calls == 1


@pytest.mark.parametrize("resp", [SimpleNamespace(choices=[]), text_resp(None), text_resp("   ")])
def test_empty_or_malformed_response_raises(resp):
    with pytest.raises(LLMResponseError):
        service(FakeClient(resp)).complete("s", "u")


def test_reasoning_model_gets_token_headroom_and_low_effort():
    client = FakeClient(text_resp("ok"))
    service(client, model="openai/gpt-oss-120b").complete("s", "u", max_tokens=600)
    sent = client.kwargs[0]
    assert sent["max_tokens"] >= 2048 and sent["extra_body"] == {"reasoning_effort": "low"}


def test_non_reasoning_model_request_is_unchanged():
    client = FakeClient(text_resp("ok"))
    service(client, model="llama-3.3-70b-versatile").complete("s", "u", max_tokens=600)
    assert client.kwargs[0]["max_tokens"] == 600 and "extra_body" not in client.kwargs[0]


def test_empty_content_with_length_finish_reports_truncation():
    resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=""))])
    with pytest.raises(LLMResponseError) as exc:
        service(FakeClient(resp)).complete("s", "u")
    assert "finish_reason=length" in str(exc.value)


def test_list_of_text_parts_content_is_extracted():
    parts = [{"type": "text", "text": '{"a":'}, SimpleNamespace(text=" 1}")]
    assert service(FakeClient(text_resp(parts))).complete("s", "u") == '{"a": 1}'
