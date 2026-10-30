import csv
import smtplib

import pytest

from outreach import config, db
from outreach import outreach_service as svc
from outreach.cli import main as cli_main
from outreach.sender import ConfigError, SendError, SimulatedSender, SMTPSender, get_sender, validate_recipient
from tests.conftest import FakeSender

SIM = SimulatedSender()


@pytest.mark.parametrize("value,ok", [("a@glow.test", True), ("Not Found", False), ("", False), (None, False),
                                      ("not-an-email", False), ("a@b", False), ("a b@c.test", False),
                                      ("x@y.test\nBcc: z@y.test", False)])
def test_validate_recipient(value, ok):
    assert (validate_recipient(value) is None) is ok


def test_simulated_send_records_once_and_never_calls_a_sender(imported):
    out = svc.send_email(imported, SIM, "UC_A")
    assert (out["status"], out["mode"], out["recipient"]) == ("simulated", "simulation", "a@glow.test")
    assert svc.send_email(imported, SIM, "UC_A")["status"] == "duplicate"
    rows = db.outreach_rows(imported, channel_id="UC_A")
    assert len(rows) == 1 and rows[0]["status"] == "simulated" and rows[0]["body"].startswith("Hi Ami")
    assert svc.send_email(imported, SIM, "UC_E")["status"] == "duplicate"        # same address as UC_A


@pytest.mark.parametrize("cid,reason", [("UC_B", "Not Found"), ("UC_C", "invalid email"),
                                        ("UC_D", "not qualified"), ("UC_G", "no usable generated message")])
def test_skipped_cases_are_recorded_and_not_repeated(imported, cid, reason):
    sender = FakeSender()
    out = svc.send_email(imported, sender, cid)
    assert out["status"] == "skipped" and reason in out["error"] and sender.calls == []
    svc.send_email(imported, sender, cid)
    assert len(db.outreach_rows(imported, channel_id=cid)) == 1


def test_unknown_influencer(imported):
    with pytest.raises(svc.NotFound):
        svc.send_email(imported, SIM, "nope")


def test_real_send_success_then_duplicate_is_not_sent_again(imported):
    sender = FakeSender()
    assert svc.send_email(imported, sender, "UC_A")["status"] == "sent"
    assert svc.send_email(imported, sender, "UC_A")["status"] == "duplicate"
    assert svc.send_email(imported, SIM, "UC_A")["status"] == "duplicate"        # no simulation after a real send
    assert sender.calls == ["a@glow.test"]
    assert db.get_influencer(imported, "UC_A")["email_status"] == "sent"


def test_failure_is_recorded_and_can_be_retried(imported):
    bad = FakeSender(error=SendError("SMTPAuthenticationError: rejected"))
    out = svc.send_email(imported, bad, "UC_A")
    assert out["status"] == "failed" and "rejected" in out["error"]
    assert svc.send_email(imported, FakeSender(), "UC_A")["status"] == "sent"
    assert [r["status"] for r in db.outreach_rows(imported, channel_id="UC_A")] == ["sent", "failed"]


def test_unexpected_exception_marks_failed_without_leaking_details(imported):
    out = svc.send_email(imported, FakeSender(error=RuntimeError("password=hunter2")), "UC_A")
    assert out["status"] == "failed" and out["error"] == "unexpected error: RuntimeError"
    assert db.get_influencer(imported, "UC_A")["email_status"] == "failed"


def test_leftover_pending_claim_blocks_resend(imported):
    db.insert_outreach(imported, "UC_A", "email", "a@glow.test", "smtp", "pending")     # e.g. crashed mid-send
    sender = FakeSender()
    assert svc.send_email(imported, sender, "UC_A")["status"] == "duplicate" and sender.calls == []


def test_real_send_allowed_after_simulation(imported):
    svc.send_email(imported, SIM, "UC_A")
    assert svc.send_email(imported, FakeSender(), "UC_A")["status"] == "sent"


def test_batch_simulation_outcomes_and_limit(imported):
    res = svc.run_email_batch(imported, SIM)
    assert res["outcomes"] == {"simulated": 2, "skipped": 2, "duplicate": 1} and res["aborted"] is None
    assert svc.run_email_batch(imported, SIM)["outcomes"] == {"duplicate": 3, "skipped": 2}   # idempotent re-run


def test_batch_limit_counts_only_real_attempts(imported):
    res = svc.run_email_batch(imported, SIM, limit=1)
    assert res["outcomes"]["simulated"] == 1


def test_batch_stops_after_consecutive_failures_even_with_skips_in_between(imported):
    sender = FakeSender(error=SendError("SMTP down"))
    res = svc.run_email_batch(imported, sender, max_consecutive_failures=2)
    assert res["outcomes"]["failed"] == 2 and "consecutive" in res["aborted"] and len(sender.calls) == 2


def test_manual_dm_workflow(imported):
    assert svc.record_dm(imported, "UC_F", "failed", "DMs closed")["status"] == "failed"
    assert svc.record_dm(imported, "UC_F", "sent", "sent from my phone")["status"] == "sent"
    with pytest.raises(db.DuplicateOutreach):
        svc.record_dm(imported, "UC_F", "sent")
    f = db.get_influencer(imported, "UC_F")
    assert f["dm_status"] == "sent" and f["dm_outreach"]["mode"] == "manual" and f["email_status"] == "not_contacted"
    for cid, exc in (("UC_D", svc.OutreachError), ("UC_G", svc.OutreachError), ("zzz", svc.NotFound)):
        with pytest.raises(exc):
            svc.record_dm(imported, cid, "sent")
    with pytest.raises(svc.OutreachError):
        svc.record_dm(imported, "UC_A", "maybe")


def test_export_tracker(imported, tmp_path):
    svc.send_email(imported, SIM, "UC_A")
    svc.record_dm(imported, "UC_A", "sent")
    out = tmp_path / "tracker.csv"
    assert svc.export_tracker(imported, out) == 6                                   # qualified influencers only
    rows = {r["Influencer"]: r for r in csv.DictReader(out.open(encoding="utf-8"))}
    assert "Failed Fay" not in rows
    a = rows["Ami Glow"]
    assert (a["Email Status"], a["Email Mode"], a["DM Status"], a["Message Generated"]) == ("simulated", "simulation", "sent", "Yes")
    assert a["Email Sent Date"] and rows["Nora Style"]["Email Status"] == "not_contacted"
    assert rows["No Message"]["Message Generated"] == "No"


# ---------------------------------------------------------------- SMTP sender (fake smtplib, no network)
class FakeSMTP:
    instances = []
    login_error = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.actions, self.sent = host, port, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.actions.append("starttls")

    def login(self, user, password):
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        self.actions.append(("login", user))

    def send_message(self, msg):
        self.sent = msg


@pytest.fixture(autouse=False)
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.login_error = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_smtp_sender_builds_message_with_tls_and_footer(fake_smtp):
    SMTPSender("smtp.test", 587, "user", "pw", "me@brand.test", footer="Reply STOP to opt out").send(
        "to@glow.test", "Hello\nthere", "Body text")
    s = fake_smtp.instances[0]
    assert s.actions == ["starttls", ("login", "user")]
    assert (s.sent["To"], s.sent["From"], s.sent["Subject"]) == ("to@glow.test", "me@brand.test", "Hello there")
    assert "Body text" in s.sent.get_content() and "Reply STOP to opt out" in s.sent.get_content()


def test_smtp_sender_port_465_uses_ssl_without_starttls(fake_smtp):
    SMTPSender("smtp.test", 465, "", "", "me@brand.test").send("to@glow.test", "S", "B")
    assert "starttls" not in fake_smtp.instances[0].actions


def test_smtp_errors_never_leak_credentials(fake_smtp):
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad credentials hunter2 for user@brand.test")
    with pytest.raises(SendError) as exc:
        SMTPSender("smtp.test", 587, "user@brand.test", "hunter2", "me@brand.test").send("t@glow.test", "S", "B")
    assert "hunter2" not in str(exc.value) and "user@brand.test" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_smtp_connection_error_becomes_send_error(monkeypatch):
    def boom(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    with pytest.raises(SendError, match="ConnectionRefusedError"):
        SMTPSender("smtp.test", 587, "", "", "me@brand.test").send("t@glow.test", "S", "B")


def test_get_sender_defaults_to_simulation_and_requires_explicit_smtp_config(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_MODE", "simulation")
    assert get_sender().mode == "simulation"
    monkeypatch.setattr(config, "EMAIL_MODE", "smtp")
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_FROM", "")
    with pytest.raises(ConfigError, match="SMTP_HOST, SMTP_FROM"):
        get_sender()
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(config, "SMTP_FROM", "me@brand.test")
    monkeypatch.setattr(config, "SMTP_USERNAME", "user")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    with pytest.raises(ConfigError, match="SMTP_PASSWORD"):
        get_sender()
    monkeypatch.setattr(config, "SMTP_PASSWORD", "pw")
    assert get_sender().mode == "smtp"
    monkeypatch.setattr(config, "EMAIL_MODE", "banana")
    with pytest.raises(ConfigError, match="must be"):
        get_sender()


# ---------------------------------------------------------------- CLI
def test_cli_flow_and_real_mode_requires_confirm(tmp_path, data_files, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(config, "EMAIL_MODE", "simulation")
    assert cli_main(["init-db"]) == 0
    assert cli_main(["import", "--influencers", str(data_files[0]), "--messages", str(data_files[1])]) == 0
    assert cli_main(["send-emails"]) == 0 and "SIMULATED" in capsys.readouterr().out
    assert cli_main(["export-tracker", "--output", str(tmp_path / "t.csv")]) == 0 and (tmp_path / "t.csv").exists()
    for name, val in (("EMAIL_MODE", "smtp"), ("SMTP_HOST", "smtp.test"), ("SMTP_FROM", "me@brand.test"),
                      ("SMTP_USERNAME", ""), ("SMTP_PASSWORD", "")):
        monkeypatch.setattr(config, name, val)
    assert cli_main(["send-emails"]) == 2 and "--confirm" in capsys.readouterr().out


def test_cli_reports_errors_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    assert cli_main(["import", "--influencers", str(tmp_path / "missing.json")]) == 1
    assert "run Phase 1" in capsys.readouterr().out
