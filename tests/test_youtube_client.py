from datetime import datetime, timezone

import pytest
import requests

from outreach.youtube_client import QuotaExceededError, YouTubeAPIError, YouTubeClient
from tests.conftest import FakeResp

WHEN = datetime(2026, 7, 1, tzinfo=timezone.utc)


class Scripted:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(session, tmp_path=None, **kw):
    return YouTubeClient("SECRET-KEY", cache_dir=tmp_path, session=session, backoff=0, **kw)


def test_requires_api_key():
    with pytest.raises(ValueError):
        YouTubeClient("")


def test_retries_transient_errors_then_succeeds():
    s = Scripted(FakeResp(500), FakeResp(429), FakeResp(200, {"items": [{"snippet": {"channelId": "A"}},
                                                                       {"snippet": {"channelId": "A"}}]}))
    ids, token = client(s).search_page("skincare", WHEN)
    assert ids == ["A"] and token is None and s.calls == 3


def test_quota_exceeded_is_distinct_error():
    body = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    with pytest.raises(QuotaExceededError):
        client(Scripted(FakeResp(403, body))).get_channels(["A"])


def test_non_retryable_error_and_key_redaction():
    with pytest.raises(YouTubeAPIError):
        client(Scripted(FakeResp(400, {"error": {"errors": [{"reason": "badRequest"}]}}))).get_channels(["A"])
    s = Scripted(*[requests.ConnectionError("failed: https://x?key=SECRET-KEY")] * 4)
    with pytest.raises(YouTubeAPIError) as exc:
        client(s).get_channels(["A"])
    assert "SECRET-KEY" not in str(exc.value)


def test_cache_prevents_repeat_network_calls(tmp_path):
    s = Scripted(FakeResp(200, {"items": [{"id": "A"}]}))
    c = client(s, tmp_path)
    assert c.get_channels(["A"]) == c.get_channels(["A"]) == [{"id": "A"}]
    assert s.calls == 1 and c.api_calls == 1
