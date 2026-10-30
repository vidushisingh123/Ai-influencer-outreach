"""Message-to-influencer matching, missing-message states, import idempotency and simulation tracking.
Uses a temporary SQLite file; no network, Groq or SMTP."""
import pytest

from outreach import db


def conn_for(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def inf(cid, name="Ami Glow", email="Not Found", **kw):
    rec = dict(channel_id=cid, name=name, platform="YouTube", profile_url=f"https://www.youtube.com/channel/{cid}",
               niche="Fashion & Beauty", follower_count=20000, engagement_rate=0.05, content_themes=["Skincare"],
               contact_email=email, filter_status="PASS", filter_reasons=["PASS platform: YouTube"])
    rec.update(kw)
    return rec


def msg(cid, tag="A", status="valid", **kw):
    m = dict(channel_id=cid, subject=f"Subject {tag}", email=f"Email body {tag}", dm=f"DM text {tag}",
             email_word_count=3, dm_word_count=3, source="llm", validation_status=status, model="m")
    m.update(kw)
    return m


def test_messages_match_by_channel_id_even_when_names_are_identical(tmp_path):
    c = conn_for(tmp_path)
    for cid in ("UC1", "UC2"):
        db.upsert_influencer(c, inf(cid, name="Ami Glow"))
    db.upsert_message(c, msg("UC1", "ONE"))
    db.upsert_message(c, msg("UC2", "TWO"))
    for cid, tag in (("UC1", "ONE"), ("UC2", "TWO")):
        got = db.get_influencer(c, cid)["message"]
        assert (got["subject"], got["email"], got["dm"]) == (f"Subject {tag}", f"Email body {tag}", f"DM text {tag}")
    listed = {r["channel_id"]: r["message"]["dm"] for r in db.list_influencers(c)}
    assert listed == {"UC1": "DM text ONE", "UC2": "DM text TWO"}


def test_influencer_without_a_message_has_none(tmp_path):
    c = conn_for(tmp_path)
    db.upsert_influencer(c, inf("UC1"))
    db.upsert_influencer(c, inf("UC2"))
    db.upsert_message(c, msg("UC2"))
    assert db.get_influencer(c, "UC1")["message"] is None
    assert db.get_influencer(c, "UC2")["message"] is not None


def test_message_for_unknown_influencer_is_skipped(tmp_path):
    c = conn_for(tmp_path)
    db.upsert_influencer(c, inf("UC1"))
    assert db.upsert_message(c, msg("UC404")) == "skipped_unknown_influencer"
    assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_unusable_message_never_replaces_a_good_one(tmp_path):
    c = conn_for(tmp_path)
    db.upsert_influencer(c, inf("UC1"))
    assert db.upsert_message(c, msg("UC1", "GOOD")) == "inserted"
    failed = msg("UC1", status="error", email=None, dm=None, subject=None, error="rate limited")
    assert db.upsert_message(c, failed) == "skipped_unusable"
    assert db.get_influencer(c, "UC1")["message"]["email"] == "Email body GOOD"


def test_reimport_is_idempotent_and_keeps_outreach_status(tmp_path):
    c = conn_for(tmp_path)
    for _ in range(2):
        db.upsert_influencer(c, inf("UC1", email="a@glow.test"))
        db.upsert_message(c, msg("UC1"))
    db.insert_outreach(c, "UC1", "email", "a@glow.test", "simulation", "simulated")
    db.insert_outreach(c, "UC1", "instagram_dm", None, "manual", "sent", note="sent by hand")
    for _ in range(2):                                   # re-import after statuses exist
        db.upsert_influencer(c, inf("UC1", email="a@glow.test"))
        assert db.upsert_message(c, msg("UC1")) == "updated"
    assert c.execute("SELECT COUNT(*) FROM influencers").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM outreach_log").fetchone()[0] == 2
    got = db.get_influencer(c, "UC1")
    assert (got["email_status"], got["dm_status"]) == ("simulated", "sent")


def test_regenerated_message_updates_in_place(tmp_path):
    c = conn_for(tmp_path)
    db.upsert_influencer(c, inf("UC1"))
    db.upsert_message(c, msg("UC1", "OLD"))
    db.upsert_message(c, msg("UC1", "NEW"))
    assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.get_influencer(c, "UC1")["message"]["subject"] == "Subject NEW"


def test_simulation_is_tracked_and_cannot_be_duplicated(tmp_path):
    c = conn_for(tmp_path)
    db.upsert_influencer(c, inf("UC1", email="a@glow.test"))
    db.upsert_influencer(c, inf("UC2", name="Other", email="A@Glow.test"))
    assert db.get_influencer(c, "UC1")["email_status"] == "not_contacted"
    db.insert_outreach(c, "UC1", "email", "a@glow.test", "simulation", "simulated")
    assert db.get_influencer(c, "UC1")["email_status"] == "simulated"
    assert db.stats(c)["outreach"]["email:simulated"] == 1
    with pytest.raises(db.DuplicateOutreach):            # same creator again
        db.insert_outreach(c, "UC1", "email", "a@glow.test", "simulation", "simulated")
    with pytest.raises(db.DuplicateOutreach):            # same address (case-insensitive), other creator
        db.insert_outreach(c, "UC2", "email", "A@Glow.test", "simulation", "simulated")
    assert db.stats(c)["outreach"]["email:simulated"] == 1
