"""SQLite persistence. Duplicate outreach is blocked by partial UNIQUE indexes (database level),
not only by application checks. Every sqlite3 failure surfaces as DatabaseError with a safe message."""
from __future__ import annotations

import functools
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS influencers (
    channel_id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL, profile_url TEXT NOT NULL,
    niche TEXT, follower_count INTEGER, engagement_rate REAL,
    content_themes TEXT NOT NULL DEFAULT '[]', contact_email TEXT NOT NULL DEFAULT 'Not Found',
    email_source TEXT, instagram_url TEXT, website TEXT, creator_country TEXT,
    filter_status TEXT NOT NULL CHECK (filter_status IN ('PASS','FAIL')),
    filter_reasons TEXT NOT NULL DEFAULT '[]', field_status TEXT NOT NULL DEFAULT '{}',
    source TEXT, last_upload_at TEXT, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    channel_id TEXT PRIMARY KEY REFERENCES influencers(channel_id) ON DELETE CASCADE,
    subject TEXT, email_body TEXT, dm TEXT, email_word_count INTEGER, dm_word_count INTEGER,
    source TEXT, validation_status TEXT NOT NULL, model TEXT, error TEXT, generated_at TEXT,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL REFERENCES influencers(channel_id),
    channel TEXT NOT NULL CHECK (channel IN ('email','instagram_dm')),
    recipient TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('simulation','smtp','manual')),
    status TEXT NOT NULL CHECK (status IN ('pending','sent','simulated','failed','skipped')),
    subject TEXT, body TEXT, error TEXT, note TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- A real/manual send (or an in-flight claim) can exist only once per influencer+channel and per email address.
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_active ON outreach_log(channel_id, channel)
    WHERE status IN ('pending','sent');
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_recipient_active ON outreach_log(lower(recipient))
    WHERE channel = 'email' AND status IN ('pending','sent');
-- Same protection for simulated runs, so re-running a simulation cannot create duplicate records.
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_simulated ON outreach_log(channel_id, channel)
    WHERE status = 'simulated';
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_recipient_simulated ON outreach_log(lower(recipient))
    WHERE channel = 'email' AND status = 'simulated';
CREATE INDEX IF NOT EXISTS ix_log_channel ON outreach_log(channel_id);
"""

STATUS_PRIORITY = ("sent", "pending", "simulated", "failed", "skipped")   # best outcome first


class DatabaseError(RuntimeError):
    """Any SQLite failure. The message is safe to show to users; details go to the log."""


class DuplicateOutreach(Exception):
    """The unique constraints say this influencer or recipient was already contacted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (sqlite3.Error, OSError) as exc:
            log.error("Database error in %s: %s", fn.__name__, exc)
            raise DatabaseError("database operation failed (see server log)") from exc
    return wrapper


@_safe
def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@_safe
def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------- imports (Phase 1 / Phase 2 data)
@_safe
def upsert_influencer(conn, rec: dict) -> None:
    with conn:
        conn.execute(
            """INSERT INTO influencers (channel_id, name, platform, profile_url, niche, follower_count,
                   engagement_rate, content_themes, contact_email, email_source, instagram_url, website,
                   creator_country, filter_status, filter_reasons, field_status, source, last_upload_at, imported_at)
               VALUES (:channel_id,:name,:platform,:profile_url,:niche,:follower_count,:engagement_rate,
                   :content_themes,:contact_email,:email_source,:instagram_url,:website,:creator_country,
                   :filter_status,:filter_reasons,:field_status,:source,:last_upload_at,:imported_at)
               ON CONFLICT(channel_id) DO UPDATE SET
                   name=excluded.name, platform=excluded.platform, profile_url=excluded.profile_url,
                   niche=excluded.niche, follower_count=excluded.follower_count,
                   engagement_rate=excluded.engagement_rate, content_themes=excluded.content_themes,
                   contact_email=excluded.contact_email, email_source=excluded.email_source,
                   instagram_url=excluded.instagram_url, website=excluded.website,
                   creator_country=excluded.creator_country, filter_status=excluded.filter_status,
                   filter_reasons=excluded.filter_reasons, field_status=excluded.field_status,
                   source=excluded.source, last_upload_at=excluded.last_upload_at,
                   imported_at=excluded.imported_at""",
            {"channel_id": rec["channel_id"], "name": rec["name"], "platform": rec.get("platform") or "YouTube",
             "profile_url": rec["profile_url"], "niche": rec.get("niche"),
             "follower_count": rec.get("follower_count"), "engagement_rate": rec.get("engagement_rate"),
             "content_themes": json.dumps(rec.get("content_themes") or []),
             "contact_email": rec.get("contact_email") or "Not Found", "email_source": rec.get("email_source"),
             "instagram_url": rec.get("instagram_url"), "website": rec.get("website"),
             "creator_country": rec.get("creator_country"), "filter_status": rec["filter_status"],
             "filter_reasons": json.dumps(rec.get("filter_reasons") or []),
             "field_status": json.dumps(rec.get("field_status") or {}), "source": rec.get("source"),
             "last_upload_at": rec.get("last_upload_at"), "imported_at": _now()})


@_safe
def upsert_message(conn, msg: dict) -> str:
    """Store a generated message under its influencer's channel_id (the only matching key).

    Returns 'inserted', 'updated', 'skipped_unusable' (no email/DM text, e.g. a failed generation) or
    'skipped_unknown_influencer'. An unusable entry never replaces a message that is already usable, and
    outreach statuses live in a different table, so re-importing cannot reset them."""
    email, dm = msg.get("email"), msg.get("dm")
    if not (isinstance(email, str) and email.strip() and isinstance(dm, str) and dm.strip()):
        return "skipped_unusable"
    cid = msg.get("channel_id")
    if conn.execute("SELECT 1 FROM influencers WHERE channel_id=?", (cid,)).fetchone() is None:
        return "skipped_unknown_influencer"
    existed = conn.execute("SELECT 1 FROM messages WHERE channel_id=?", (cid,)).fetchone() is not None
    with conn:
        conn.execute(
            """INSERT INTO messages (channel_id, subject, email_body, dm, email_word_count, dm_word_count,
                   source, validation_status, model, error, generated_at, imported_at)
               VALUES (:channel_id,:subject,:email_body,:dm,:email_word_count,:dm_word_count,:source,
                   :validation_status,:model,:error,:generated_at,:imported_at)
               ON CONFLICT(channel_id) DO UPDATE SET
                   subject=excluded.subject, email_body=excluded.email_body, dm=excluded.dm,
                   email_word_count=excluded.email_word_count, dm_word_count=excluded.dm_word_count,
                   source=excluded.source, validation_status=excluded.validation_status, model=excluded.model,
                   error=excluded.error, generated_at=excluded.generated_at, imported_at=excluded.imported_at""",
            {"channel_id": cid, "subject": msg.get("subject"), "email_body": email,
             "dm": dm, "email_word_count": msg.get("email_word_count"),
             "dm_word_count": msg.get("dm_word_count"), "source": msg.get("source"),
             "validation_status": msg.get("validation_status") or "unknown", "model": msg.get("model"),
             "error": msg.get("error"), "generated_at": msg.get("generated_at"), "imported_at": _now()})
    return "updated" if existed else "inserted"


# ---------------------------------------------------------------- reads
def _best_outreach(conn) -> dict[tuple[str, str], dict]:
    """Best outcome per (influencer, channel); later rows win ties."""
    best: dict[tuple[str, str], dict] = {}
    for r in conn.execute("SELECT id, channel_id, channel, mode, status, error, note, updated_at "
                          "FROM outreach_log ORDER BY id"):
        key = (r["channel_id"], r["channel"])
        if key not in best or STATUS_PRIORITY.index(r["status"]) <= STATUS_PRIORITY.index(best[key]["status"]):
            best[key] = dict(r)
    return best


def _hydrate(row: sqlite3.Row, best: dict) -> dict:
    d = dict(row)
    for col in ("content_themes", "filter_reasons", "field_status"):
        d[col] = json.loads(d[col])
    msg_cols = ("subject", "email_body", "dm", "email_word_count", "dm_word_count", "msg_source",
                "validation_status", "msg_error", "generated_at")
    if d.get("validation_status") is None:
        d["message"] = None
    else:
        d["message"] = {"subject": d["subject"], "email": d["email_body"], "dm": d["dm"],
                        "email_word_count": d["email_word_count"], "dm_word_count": d["dm_word_count"],
                        "source": d["msg_source"], "validation_status": d["validation_status"],
                        "error": d["msg_error"], "generated_at": d["generated_at"]}
    for col in msg_cols:
        d.pop(col, None)
    email, dm = best.get((d["channel_id"], "email")), best.get((d["channel_id"], "instagram_dm"))
    d["email_outreach"], d["dm_outreach"] = email, dm
    d["email_status"] = email["status"] if email else "not_contacted"
    d["dm_status"] = dm["status"] if dm else "not_contacted"
    return d


_SELECT = """SELECT i.*, m.subject, m.email_body, m.dm, m.email_word_count, m.dm_word_count,
                    m.source AS msg_source, m.validation_status, m.error AS msg_error, m.generated_at
             FROM influencers i LEFT JOIN messages m USING (channel_id)"""


@_safe
def list_influencers(conn, qualification: str | None = None, platform: str | None = None,
                     email_status: str | None = None, dm_status: str | None = None) -> list[dict]:
    where, params = [], []
    if qualification:
        where.append("i.filter_status = ?"); params.append(qualification.upper())
    if platform:
        where.append("i.platform = ?"); params.append(platform)
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY i.filter_status DESC, i.follower_count DESC, i.name"
    best = _best_outreach(conn)
    items = [_hydrate(r, best) for r in conn.execute(sql, params)]
    if email_status:
        items = [i for i in items if i["email_status"] == email_status]
    if dm_status:
        items = [i for i in items if i["dm_status"] == dm_status]
    return items


@_safe
def get_influencer(conn, channel_id: str) -> dict | None:
    row = conn.execute(_SELECT + " WHERE i.channel_id = ?", (channel_id,)).fetchone()
    return _hydrate(row, _best_outreach(conn)) if row else None


@_safe
def outreach_rows(conn, channel_id: str | None = None, channel: str | None = None,
                  status: str | None = None, limit: int = 200) -> list[dict]:
    where, params = [], []
    for col, val in (("l.channel_id", channel_id), ("l.channel", channel), ("l.status", status)):
        if val:
            where.append(f"{col} = ?"); params.append(val)
    sql = ("SELECT l.*, i.name FROM outreach_log l JOIN influencers i USING (channel_id)"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY l.id DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, (*params, limit))]


@_safe
def stats(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    out = {
        "influencers": one("SELECT COUNT(*) FROM influencers"),
        "qualified": one("SELECT COUNT(*) FROM influencers WHERE filter_status='PASS'"),
        "qualified_with_message": one("SELECT COUNT(*) FROM influencers i JOIN messages m USING (channel_id) "
                                      "WHERE i.filter_status='PASS'"),
        "qualified_with_email": one("SELECT COUNT(*) FROM influencers WHERE filter_status='PASS' "
                                    "AND contact_email != 'Not Found'"),
        "outreach": {},
    }
    for r in conn.execute("SELECT channel, status, COUNT(*) n FROM outreach_log GROUP BY channel, status"):
        out["outreach"][f"{r['channel']}:{r['status']}"] = r["n"]
    return out


# ---------------------------------------------------------------- outreach writes
@_safe
def insert_outreach(conn, channel_id: str, channel: str, recipient: str | None, mode: str, status: str,
                    subject: str | None = None, body: str | None = None, error: str | None = None,
                    note: str | None = None) -> int:
    now = _now()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO outreach_log (channel_id, channel, recipient, mode, status, subject, body, error,"
                " note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (channel_id, channel, recipient, mode, status, subject, body, error, note, now, now))
        return cur.lastrowid
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc).upper():
            raise DuplicateOutreach("this influencer or recipient was already contacted") from exc
        raise


@_safe
def finish_outreach(conn, row_id: int, status: str, error: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE outreach_log SET status=?, error=?, updated_at=? WHERE id=?",
                     (status, error, _now(), row_id))


@_safe
def has_active(conn, channel_id: str, channel: str) -> bool:
    return conn.execute("SELECT 1 FROM outreach_log WHERE channel_id=? AND channel=? "
                        "AND status IN ('pending','sent') LIMIT 1", (channel_id, channel)).fetchone() is not None


@_safe
def record_skipped(conn, channel_id: str, channel: str, recipient: str | None, mode: str, reason: str,
                   subject: str | None = None) -> int:
    """One skipped row per distinct reason; repeating the same skip only refreshes its timestamp."""
    last = conn.execute("SELECT id, status, error FROM outreach_log WHERE channel_id=? AND channel=? "
                        "ORDER BY id DESC LIMIT 1", (channel_id, channel)).fetchone()
    if last and last["status"] == "skipped" and last["error"] == reason:
        with conn:
            conn.execute("UPDATE outreach_log SET updated_at=? WHERE id=?", (_now(), last["id"]))
        return last["id"]
    return insert_outreach(conn, channel_id, channel, recipient, mode, "skipped", subject, None, reason)
