import pytest

from outreach import db
from outreach import outreach_service as svc


def test_init_creates_tables_and_unique_indexes_and_is_idempotent(conn):
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"influencers", "messages", "outreach_log", "uq_log_active", "uq_log_recipient_active",
            "uq_log_simulated", "uq_log_recipient_simulated"} <= names


def test_import_counts_and_failed_influencers_get_no_messages(conn, data_files):
    counts = svc.import_from_files(conn, *data_files)
    assert counts == {"influencers": 7, "qualified": 6, "invalid_records": 1, "messages": 5, "messages_ignored": 1}
    assert db.get_influencer(conn, "UC_D")["message"] is None
    assert db.get_influencer(conn, "UC_A")["message"]["email"].startswith("Hi Ami")
    svc.import_from_files(conn, *data_files)                          # re-import is an upsert, not a duplicate
    assert db.stats(conn)["influencers"] == 7


def test_import_reports_missing_inputs(conn, tmp_path, data_files):
    with pytest.raises(svc.OutreachError, match="run Phase 1"):
        svc.import_from_files(conn, tmp_path / "nope.json", None)
    counts = svc.import_from_files(conn, data_files[0], tmp_path / "no_messages.json")
    assert "run Phase 2" in counts["warning"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(svc.OutreachError, match="not valid JSON"):
        svc.import_from_files(conn, bad, None)


def test_database_blocks_duplicate_sends_even_without_app_checks(imported):
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "sent")
    with pytest.raises(db.DuplicateOutreach):
        db.insert_outreach(imported, "UC_A", "email", "other@glow.test", "smtp", "sent")      # same influencer
    with pytest.raises(db.DuplicateOutreach):
        db.insert_outreach(imported, "UC_E", "email", "A@GLOW.test", "smtp", "pending")      # same address, any case
    db.insert_outreach(imported, "UC_A", "instagram_dm", None, "manual", "sent")              # other channel is separate
    with pytest.raises(db.DuplicateOutreach):
        db.insert_outreach(imported, "UC_A", "instagram_dm", None, "manual", "sent")


def test_failed_attempts_do_not_block_retry_but_simulations_are_unique(imported):
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "failed", error="boom")
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "failed", error="boom")
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "simulation", "simulated")
    with pytest.raises(db.DuplicateOutreach):
        db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "simulation", "simulated")
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "sent")             # real send after simulation


def test_pending_claim_blocks_second_claim_and_finish_frees_or_confirms(imported):
    row = db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "pending")
    with pytest.raises(db.DuplicateOutreach):
        db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "pending")
    db.finish_outreach(imported, row, "failed", "smtp down")
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "pending")           # retry now possible


def test_database_errors_are_wrapped_safely(tmp_path):
    c = db.connect(tmp_path / "x.db")
    c.close()
    with pytest.raises(db.DatabaseError) as exc:
        db.stats(c)                                                   # closed connection
    assert "Cannot operate" not in str(exc.value)                     # raw sqlite text is not exposed
    with pytest.raises(db.DatabaseError):
        db.connect(tmp_path)                                          # a directory is not a database


def test_invalid_rows_are_rejected_by_check_constraints(conn):
    with pytest.raises(db.DatabaseError):
        db.upsert_influencer(conn, {"channel_id": "X", "name": "X", "profile_url": "https://x.test",
                                    "filter_status": "MAYBE"})


def test_skipped_rows_are_not_repeated(imported):
    first = db.record_skipped(imported, "UC_B", "email", "Not Found", "simulation", "no contact email (Not Found)")
    again = db.record_skipped(imported, "UC_B", "email", "Not Found", "simulation", "no contact email (Not Found)")
    assert first == again and len(db.outreach_rows(imported, channel_id="UC_B")) == 1


def test_list_filters_and_status_rollup(imported):
    assert {i["channel_id"] for i in db.list_influencers(imported, qualification="FAIL")} == {"UC_D"}
    assert db.list_influencers(imported, platform="TikTok") == []
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "simulation", "simulated")
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "failed", error="x")
    a = db.get_influencer(imported, "UC_A")
    assert a["email_status"] == "simulated" and a["dm_status"] == "not_contacted"   # best outcome wins
    assert [i["channel_id"] for i in db.list_influencers(imported, email_status="simulated")] == ["UC_A"]
