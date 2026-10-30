"""Read-only submission check against a RUNNING API. Nothing is changed, sent or generated.

Run:  python -m outreach.verify [--total 391 --qualified 53 --contactable 24]
"""
from __future__ import annotations

import argparse
import os

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def check(stats: dict, rows: list[dict], cfg: dict, total: int = 391, qualified: int = 53,
          contactable: int = 24) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(ok: bool, text: str, level: str = FAIL) -> None:
        out.append((PASS if ok else level, text))

    add(stats["influencers"] == total, f"total creators = {stats['influencers']} (expected {total})")
    add(stats["qualified"] == qualified, f"qualified = {stats['qualified']} (expected {qualified})")
    add(stats["qualified_with_email"] == contactable,
        f"qualified with email = {stats['qualified_with_email']} (expected {contactable})")
    add(len(rows) == stats["influencers"], f"list returns {len(rows)} creators (stats say {stats['influencers']})")
    ids = [r["channel_id"] for r in rows]
    add(len(ids) == len(set(ids)), "no duplicate creators")
    with_msg = [r for r in rows if (r.get("message") or {}).get("email")]
    qualified_msgs = [r for r in with_msg if r["filter_status"] == "PASS"]
    add(len(qualified_msgs) == stats["qualified_with_message"],
        f"{len(qualified_msgs)} qualified creators have a message (stats say {stats['qualified_with_message']})")
    stray = [r["name"] for r in with_msg if r["name"] not in r["message"]["email"]]
    add(not stray, "every message addresses its own creator" if not stray
        else f"message does not mention the creator's name for: {', '.join(stray)}", WARN)
    bad = [r["name"] for r in rows if r["filter_status"] != "PASS"
           and (r.get("email_status") != "not_contacted" or r.get("dm_status") != "not_contacted")]
    add(not bad, "no outreach recorded for unqualified creators" if not bad else f"outreach on unqualified: {bad}")
    add(not cfg.get("real_sending"), "simulation mode is on (real sending is OFF)", WARN)
    return out


def main(argv=None) -> int:
    import requests
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default=os.getenv("API_URL", "http://127.0.0.1:8000"))
    p.add_argument("--total", type=int, default=391)
    p.add_argument("--qualified", type=int, default=53)
    p.add_argument("--contactable", type=int, default=24)
    a = p.parse_args(argv)
    try:
        get = lambda path: requests.get(a.api.rstrip("/") + path, timeout=30).json()  # noqa: E731
        results = check(get("/stats"), get("/influencers"), get("/config"), a.total, a.qualified, a.contactable)
    except requests.RequestException as exc:
        print(f"Cannot reach the API at {a.api}: {type(exc).__name__}. Start it first.")
        return 2
    for level, text in results:
        print(f"[{level}] {text}")
    return 1 if any(level == FAIL for level, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
