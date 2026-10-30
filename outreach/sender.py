"""Email sending layer. Simulation is the default; real SMTP needs EMAIL_MODE=smtp plus SMTP_* settings."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from . import config
from .models import EMAIL_RE, NOT_FOUND

log = logging.getLogger(__name__)


class SendError(RuntimeError):
    """Delivery failed. The message never contains credentials."""


class ConfigError(RuntimeError):
    """Sending is misconfigured (names the missing settings, never their values)."""


def validate_recipient(email: str | None) -> str | None:
    """Return the reason this address must not be emailed, or None if it is acceptable."""
    if not email or email.strip() == NOT_FOUND:
        return "no contact email (Not Found)"
    e = email.strip()
    if len(e) > 254 or not EMAIL_RE.fullmatch(e):
        return "invalid email address format"
    return None


class SimulatedSender:
    mode = "simulation"
    label = "SIMULATED (nothing is sent)"

    def send(self, to: str, subject: str, body: str) -> None:
        log.info("[simulation] would send email to %s", to)


class SMTPSender:
    mode = "smtp"
    label = "REAL SMTP (emails are actually sent)"

    def __init__(self, host: str, port: int, username: str, password: str, from_addr: str,
                 footer: str = "", timeout: int = 15):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.from_addr, self.footer, self.timeout = from_addr, footer, timeout

    def _redact(self, text: str) -> str:
        for secret in (self.password, self.username):
            if secret:
                text = text.replace(secret, "***")
        return text

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.from_addr, to, " ".join(subject.split())
        msg.set_content(body + (f"\n\n{self.footer}" if self.footer else ""))
        try:
            if self.port == 465:
                conn = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                        context=ssl.create_default_context())
            else:
                conn = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with conn as server:
                if self.port != 465:
                    server.starttls(context=ssl.create_default_context())
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(self._redact(f"{type(exc).__name__}: {exc}")[:300]) from None


def get_sender():
    """Simulation unless EMAIL_MODE=smtp is set explicitly (and fully configured)."""
    mode = config.EMAIL_MODE
    if mode == "simulation":
        return SimulatedSender()
    if mode != "smtp":
        raise ConfigError(f"EMAIL_MODE must be 'simulation' or 'smtp' (got {mode!r})")
    missing = [n for n, v in (("SMTP_HOST", config.SMTP_HOST), ("SMTP_FROM", config.SMTP_FROM)) if not v]
    if config.SMTP_USERNAME and not config.SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if missing:
        raise ConfigError("EMAIL_MODE=smtp but these settings are missing: " + ", ".join(missing))
    return SMTPSender(config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USERNAME, config.SMTP_PASSWORD,
                      config.SMTP_FROM, config.EMAIL_FOOTER)
