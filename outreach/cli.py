"""Command line: database setup, import, simulated/real email run, tracker export.

  python -m outreach.cli init-db
  python -m outreach.cli import
  python -m outreach.cli send-emails [--limit N] [--confirm]   (simulation unless EMAIL_MODE=smtp)
  python -m outreach.cli export-tracker
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import config, db, outreach_service as svc
from .sender import ConfigError, get_sender


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Outreach database and sending tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the SQLite database and tables")
    imp = sub.add_parser("import", help="load influencers_all.json and messages.json into SQLite")
    imp.add_argument("--influencers", type=Path)
    imp.add_argument("--messages", type=Path)
    send = sub.add_parser("send-emails", help="send (or simulate) emails to qualified influencers")
    send.add_argument("--limit", type=int)
    send.add_argument("--confirm", action="store_true", help="required when EMAIL_MODE=smtp (real sending)")
    exp = sub.add_parser("export-tracker", help="write the outreach tracker CSV")
    exp.add_argument("--output", type=Path, default=config.DATA_DIR / "outreach_tracker.csv")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        conn = db.connect(config.DB_PATH)
        db.init_schema(conn)
        if args.cmd == "init-db":
            print(f"Database ready: {config.DB_PATH}")
        elif args.cmd == "import":
            print(json.dumps(svc.import_from_files(conn, args.influencers, args.messages), indent=2))
        elif args.cmd == "send-emails":
            sender = get_sender()
            if sender.mode == "smtp" and not args.confirm:
                print("EMAIL_MODE=smtp would send REAL emails. Re-run with --confirm to proceed.")
                return 2
            print(f"Mode: {sender.label}")
            result = svc.run_email_batch(conn, sender, args.limit)
            print(json.dumps(result, indent=2))
            return 1 if result["aborted"] else 0
        elif args.cmd == "export-tracker":
            print(f"Wrote {svc.export_tracker(conn, args.output)} rows to {args.output}")
    except (db.DatabaseError, ConfigError, svc.OutreachError) as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
