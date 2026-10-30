"""Test helpers. The payloads below are synthetic TEST FIXTURES for exercising code paths offline.
They are never written to data/ and are not part of the influencer dataset."""
from datetime import datetime, timedelta, timezone

import pytest


class FakeResp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code, self._data, self.text = status, data if data is not None else {}, text
        self.headers = headers or {}

    def json(self):
        return self._data


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_channel(cid, title, subs, desc="", country=None, hidden=False, keywords=""):
    stats = {"hiddenSubscriberCount": True} if hidden else {"subscriberCount": str(subs)}
    sn = {"title": title, "description": desc}
    if country:
        sn["country"] = country
    return {"id": cid, "snippet": sn, "statistics": stats,
            "contentDetails": {"relatedPlaylists": {"uploads": f"UU_{cid}"}},
            "brandingSettings": {"channel": {"keywords": keywords}}}


def make_videos(cid, titles, views=1000, likes=50, comments=10, days_ago=5, hide_likes=False):
    out = []
    for i, t in enumerate(titles):
        st = {"viewCount": str(views), "commentCount": str(comments)}
        if not hide_likes:
            st["likeCount"] = str(likes)
        out.append({"id": f"{cid}_v{i}", "snippet": {"title": t, "publishedAt": iso(days_ago + i), "tags": []},
                    "statistics": st})
    return out


class FakeYouTube:
    """Routes requests by endpoint like the real API would."""

    def __init__(self, channels, videos):
        self.channels = {c["id"]: c for c in channels}
        self.videos = videos          # channel id -> list of video items
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == "search":
            return FakeResp(200, {"items": [{"snippet": {"channelId": c}} for c in self.channels]})
        if endpoint == "channels":
            ids = params["id"].split(",")
            return FakeResp(200, {"items": [self.channels[i] for i in ids if i in self.channels]})
        if endpoint == "playlistItems":
            cid = params["playlistId"].removeprefix("UU_")
            return FakeResp(200, {"items": [{"contentDetails": {"videoId": v["id"]}} for v in self.videos[cid]]})
        if endpoint == "videos":
            wanted = set(params["id"].split(","))
            return FakeResp(200, {"items": [v for vs in self.videos.values() for v in vs if v["id"] in wanted]})
        return FakeResp(404, {"error": {"errors": [{"reason": "notFound"}]}})


@pytest.fixture
def beauty_titles():
    return ["My nightly skincare routine", "Easy everyday makeup tutorial", "Spring outfit ideas lookbook",
            "Drugstore foundation review", "Hair care routine for curls"]


# ======================= Phase 3 fixtures (synthetic test data; never written to data/) =======================
import json  # noqa: E402

from outreach import db  # noqa: E402
from outreach.outreach_service import import_from_files  # noqa: E402


def influencer_json(cid, name, status="PASS", email="Not Found", instagram=None):
    return {"channel_id": cid, "name": name, "platform": "YouTube",
            "profile_url": f"https://www.youtube.com/channel/{cid}", "niche": "Fashion & Beauty",
            "follower_count": 20000, "engagement_rate": 0.05, "content_themes": ["Skincare"],
            "contact_email": email, "email_source": "public_channel_description" if "@" in email else "Not Found",
            "instagram_url": instagram, "filter_status": status,
            "filter_reasons": [f"{status} followers: 20,000"], "field_status": {}, "source": "YouTube Data API v3"}


def message_json(cid, status="valid", source="llm", **over):
    msg = {"channel_id": cid, "subject": f"Collab idea {cid}", "email": f"Hi there, email body for {cid}.",
           "dm": f"Hi, dm for {cid}", "email_word_count": 75, "dm_word_count": 20, "source": source,
           "validation_status": status, "model": "fake", "error": None}
    msg.update(over)
    return msg


@pytest.fixture
def data_files(tmp_path):
    infs = [influencer_json("A", "Ami Glow", email="a@glow.test", instagram="https://www.instagram.com/ami.glow/"),
            influencer_json("B", "Nora", email="Not Found"),
            influencer_json("C", "Bad Email", email="not-an-email"),
            influencer_json("D", "Failed Fay", status="FAIL", email="d@x.test"),
            influencer_json("E", "Twin", email="A@Glow.test"),
            influencer_json("F", "Fallback Fran", email="f@x.test"),
            influencer_json("G", "Errored Eli", email="g@x.test"),
            {"name": "record without id"}]
    msgs = [message_json("A"), message_json("B"), message_json("C"), message_json("D"), message_json("E"),
            message_json("F", status="fallback_insufficient_context", source="fallback_template"),
            message_json("G", status="error", email=None, dm=None, subject=None, source=None)]
    (tmp_path / "influencers_all.json").write_text(json.dumps(infs))
    (tmp_path / "messages.json").write_text(json.dumps({"messages": msgs}))
    return tmp_path / "influencers_all.json", tmp_path / "messages.json"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def seeded(conn, data_files):
    import_from_files(conn, *data_files)
    return conn


class RecordingSender:
    def __init__(self, mode="smtp", error=None):
        self.mode, self.error, self.sent = mode, error, []

    def send(self, to, subject, body):
        if self.error:
            raise self.error
        self.sent.append((to, subject, body))


# ---------------------------------------------------------------- Phase 3 fixtures (synthetic, tmp_path only)
import json as _json
from outreach import db as _db


def _inf(cid, name, status="PASS", email="Not Found", followers=10000, instagram=None):
    return {"channel_id": cid, "name": name, "platform": "YouTube", "profile_url": f"https://www.youtube.com/channel/{cid}",
            "follower_count": followers, "engagement_rate": 0.05, "niche": "Fashion & Beauty",
            "content_themes": ["Skincare"], "contact_email": email, "email_source": "public_channel_description"
            if email != "Not Found" else "Not Found", "instagram_url": instagram, "website": None,
            "creator_country": None, "filter_status": status, "last_upload_at": "2026-10-01T00:00:00Z",
            "filter_reasons": [f"{status} followers: {followers:,}"], "field_status": {"follower_count": "api_verified"},
            "source": "YouTube Data API v3"}


def _msg(cid, name, status="valid", source="llm"):
    return {"channel_id": cid, "name": name, "subject": f"Collab idea for {name}", "email": f"Hi {name}, email body",
            "dm": f"Hi {name}, DM text", "email_word_count": 75, "dm_word_count": 22, "source": source,
            "validation_status": status, "model": "fake", "error": None, "generated_at": "2026-10-28T00:00:00+00:00"}


@pytest.fixture
def data_files(tmp_path):
    influencers = [
        _inf("UC_A", "Ami Glow", email="a@glow.test", followers=50000, instagram="https://www.instagram.com/ami.glow/"),
        _inf("UC_B", "Nora Style", followers=45000),
        _inf("UC_C", "Bad Email", email="not-an-email", followers=44000),
        _inf("UC_D", "Failed Fay", status="FAIL", email="f@x.test", followers=900),
        _inf("UC_E", "Ami Twin", email="A@glow.test", followers=40000),          # same address as UC_A
        _inf("UC_F", "Fallback Fran", email="f2@glow.test", followers=30000),
        _inf("UC_G", "No Message", email="g@glow.test", followers=20000),
        {"name": "Broken record without id"},
    ]
    messages = [_msg("UC_A", "Ami Glow"), _msg("UC_B", "Nora Style"), _msg("UC_C", "Bad Email"),
                _msg("UC_D", "Failed Fay"), _msg("UC_E", "Ami Twin"),
                _msg("UC_F", "Fallback Fran", "fallback_after_invalid_output", "fallback_template")]
    inf_path, msg_path = tmp_path / "influencers_all.json", tmp_path / "messages.json"
    inf_path.write_text(_json.dumps(influencers))
    msg_path.write_text(_json.dumps({"messages": messages}))
    return inf_path, msg_path


@pytest.fixture
def conn(tmp_path):
    c = _db.connect(tmp_path / "test.db")
    _db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def imported(conn, data_files):
    from outreach import outreach_service as svc
    svc.import_from_files(conn, *data_files)
    return conn


class FakeSender:
    """Stands in for SMTP: records calls, optionally raises."""

    def __init__(self, mode="smtp", error=None):
        self.mode, self.label, self.error, self.calls = mode, f"fake {mode}", error, []

    def send(self, to, subject, body):
        self.calls.append(to)
        if self.error:
            raise self.error
