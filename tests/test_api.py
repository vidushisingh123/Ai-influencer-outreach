import pytest
from fastapi.testclient import TestClient

from outreach import config, db
from outreach.api import create_app
from outreach.sender import ConfigError, SimulatedSender
from tests.conftest import FakeSender


@pytest.fixture
def make_client(tmp_path, data_files, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)          # /import reads influencers_all.json + messages.json here

    def build(sender=None, factory=None):
        app = create_app(tmp_path / "api.db", sender_factory=factory or (lambda: sender or SimulatedSender()))
        return TestClient(app)
    return build


@pytest.fixture
def client(make_client):
    c = make_client()
    assert c.post("/import").status_code == 200
    return c


def test_health_config_and_no_secrets(make_client, monkeypatch):
    monkeypatch.setattr(config, "SMTP_PASSWORD", "topsecret-pw")
    c = make_client()
    assert c.get("/health").json() == {"status": "ok"}
    cfg = c.get("/config").json()
    assert cfg["email_mode"] == "simulation" and cfg["real_sending"] is False and "topsecret-pw" not in str(cfg)
    real = make_client(sender=FakeSender("smtp")).get("/config").json()
    assert real["real_sending"] is True and real["email_mode"] == "smtp" and "topsecret-pw" not in str(real)


def test_misconfigured_sender_gives_clear_400(make_client):
    def bad():
        raise ConfigError("EMAIL_MODE=smtp but these settings are missing: SMTP_HOST")
    r = make_client(factory=bad).get("/config")
    assert r.status_code == 400 and "SMTP_HOST" in r.json()["detail"]


def test_import_list_and_filters(client):
    rows = client.get("/influencers").json()
    assert len(rows) == 7 and rows[0]["filter_status"] == "PASS"                 # qualified first
    fail = client.get("/influencers", params={"qualification": "FAIL"}).json()
    assert [r["name"] for r in fail] == ["Failed Fay"] and fail[0]["message"] is None
    assert client.get("/influencers", params={"platform": "TikTok"}).json() == []
    assert client.get("/influencers", params={"qualification": "MAYBE"}).status_code == 422
    first = client.get("/influencers", params={"qualification": "PASS"}).json()[0]
    assert {"name", "platform", "profile_url", "niche", "follower_count", "engagement_rate", "contact_email",
            "filter_reasons", "message", "email_status", "dm_status"} <= set(first)


def test_import_without_phase1_output_is_a_clear_error(make_client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "empty")
    r = make_client().post("/import")
    assert r.status_code == 400 and "run Phase 1" in r.json()["detail"]


def test_simulated_email_flow_and_status_filter(client):
    assert client.post("/outreach/email/UC_A").json()["status"] == "simulated"
    assert client.post("/outreach/email/UC_A").json()["status"] == "duplicate"
    assert client.post("/outreach/email/UC_B").json()["status"] == "skipped"
    assert client.post("/outreach/email/nope").status_code == 404
    sim = client.get("/influencers", params={"email_status": "simulated"}).json()
    assert [r["channel_id"] for r in sim] == ["UC_A"]
    detail = client.get("/influencers/UC_A").json()
    assert detail["history"][0]["status"] == "simulated" and client.get("/influencers/zzz").status_code == 404


def test_real_sending_needs_explicit_confirmation(make_client):
    sender = FakeSender("smtp")
    c = make_client(sender=sender)
    c.post("/import")
    r = c.post("/outreach/email/UC_A")
    assert r.status_code == 400 and "confirm=true" in r.json()["detail"] and sender.calls == []
    assert c.post("/outreach/email/run", json={}).status_code == 400
    assert c.post("/outreach/email/UC_A", json={"confirm": True}).json()["status"] == "sent"
    assert c.post("/outreach/email/UC_A", json={"confirm": True}).json()["status"] == "duplicate"
    assert sender.calls == ["a@glow.test"]


def test_batch_endpoint(client):
    res = client.post("/outreach/email/run", json={"limit": 1}).json()
    assert res["mode"] == "simulation" and res["outcomes"]["simulated"] == 1
    assert client.post("/outreach/email/run", json={"limit": 0}).status_code == 422


def test_manual_dm_status_endpoints(client):
    ok = client.post("/outreach/dm/UC_A/status", json={"status": "sent", "note": "sent by hand"})
    assert ok.status_code == 200 and ok.json()["mode"] == "manual"
    assert client.post("/outreach/dm/UC_A/status", json={"status": "sent"}).status_code == 409
    assert client.post("/outreach/dm/UC_A/status", json={"status": "bogus"}).status_code == 422
    assert client.post("/outreach/dm/UC_D/status", json={"status": "sent"}).status_code == 400
    assert client.post("/outreach/dm/zzz/status", json={"status": "sent"}).status_code == 404
    assert client.get("/influencers/UC_A").json()["dm_status"] == "sent"


def test_log_and_stats(client):
    client.post("/outreach/email/UC_A")
    client.post("/outreach/dm/UC_A/status", json={"status": "sent"})
    log = client.get("/outreach/log").json()
    assert {(r["channel"], r["status"]) for r in log} == {("email", "simulated"), ("instagram_dm", "sent")}
    assert client.get("/outreach/log", params={"channel": "email"}).json()[0]["name"] == "Ami Glow"
    s = client.get("/stats").json()
    assert s["qualified"] == 6 and s["qualified_with_message"] == 5 and s["outreach"]["email:simulated"] == 1


def test_database_failure_returns_generic_500(client, monkeypatch):
    def boom(*a, **k):
        raise db.DatabaseError("database operation failed (see server log)")
    monkeypatch.setattr(db, "list_influencers", boom)
    r = client.get("/influencers")
    assert r.status_code == 500 and r.json() == {"detail": "Database error - check the server log."}
