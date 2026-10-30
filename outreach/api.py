"""FastAPI backend.  Run:  uvicorn outreach.api:create_app --factory --reload"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, db, outreach_service as svc
from .sender import ConfigError, get_sender

log = logging.getLogger("outreach.api")


class ConfirmBody(BaseModel):
    confirm: bool = False


class BatchBody(ConfirmBody):
    limit: int | None = Field(default=None, ge=1, le=500)


class DMStatusBody(BaseModel):
    status: Literal["sent", "failed"]
    note: str | None = Field(default=None, max_length=500)


def create_app(db_path: Path | str | None = None, sender_factory=None) -> FastAPI:
    app = FastAPI(title="EDXSO Outreach API", version="1.0")
    path = Path(db_path or config.DB_PATH)
    make_sender = sender_factory or get_sender
    _boot = db.connect(path)
    db.init_schema(_boot)
    _boot.close()

    def get_conn():
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    def _sender(confirm: bool):
        sender = make_sender()
        if sender.mode == "smtp" and not confirm:
            raise svc.OutreachError("EMAIL_MODE=smtp sends REAL emails: repeat the request with confirm=true")
        return sender

    def _err(status: int, detail: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": detail})

    @app.exception_handler(db.DatabaseError)
    async def _db_error(_: Request, exc: db.DatabaseError):
        return _err(500, "Database error - check the server log.")

    @app.exception_handler(svc.NotFound)
    async def _not_found(_: Request, exc: svc.NotFound):
        return _err(404, str(exc))

    @app.exception_handler(db.DuplicateOutreach)
    async def _dup(_: Request, exc: db.DuplicateOutreach):
        return _err(409, str(exc))

    @app.exception_handler(svc.OutreachError)
    async def _bad(_: Request, exc: svc.OutreachError):
        return _err(400, str(exc))

    @app.exception_handler(ConfigError)
    async def _cfg(_: Request, exc: ConfigError):
        return _err(400, str(exc))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/config")
    def get_config():
        sender = make_sender()          # raises ConfigError (-> 400) if EMAIL_MODE is misconfigured
        return {"email_mode": sender.mode, "email_mode_label": sender.label, "real_sending": sender.mode == "smtp",
                "dm_mode": "manual (copy the DM, send it yourself, then record the outcome)"}

    @app.get("/stats")
    def stats(conn=Depends(get_conn)):
        return db.stats(conn)

    @app.post("/import")
    def import_data(conn=Depends(get_conn)):
        return svc.import_from_files(conn)

    @app.get("/influencers")
    def list_influencers(qualification: Literal["PASS", "FAIL"] | None = None, platform: str | None = None,
                         email_status: str | None = None, dm_status: str | None = None, conn=Depends(get_conn)):
        return db.list_influencers(conn, qualification, platform, email_status, dm_status)

    @app.get("/influencers/{channel_id}")
    def get_influencer(channel_id: str, conn=Depends(get_conn)):
        inf = db.get_influencer(conn, channel_id)
        if inf is None:
            raise svc.NotFound(f"unknown influencer {channel_id!r}")
        inf["history"] = db.outreach_rows(conn, channel_id=channel_id)
        return inf

    @app.post("/outreach/email/run")
    def run_batch(body: BatchBody, conn=Depends(get_conn)):
        return svc.run_email_batch(conn, _sender(body.confirm), body.limit)

    @app.post("/outreach/email/{channel_id}")
    def send_one(channel_id: str, body: ConfirmBody | None = None, conn=Depends(get_conn)):
        return svc.send_email(conn, _sender(body.confirm if body else False), channel_id)

    @app.post("/outreach/dm/{channel_id}/status")
    def dm_status(channel_id: str, body: DMStatusBody, conn=Depends(get_conn)):
        return svc.record_dm(conn, channel_id, body.status, body.note)

    @app.get("/outreach/log")
    def log_rows(channel: Literal["email", "instagram_dm"] | None = None, status: str | None = None,
                 limit: int = Query(200, ge=1, le=1000), conn=Depends(get_conn)):
        return db.outreach_rows(conn, channel=channel, status=status, limit=limit)

    return app
