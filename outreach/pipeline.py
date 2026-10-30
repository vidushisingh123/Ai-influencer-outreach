"""Phase 1 pipeline: discovery -> collection -> enrichment -> filtering -> dataset files.

Run:  python -m outreach.pipeline --niche fashion_beauty
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from . import config
from .config import NICHES, FilterCriteria, Niche
from .enrichment import ThemeMatcher, build_influencer, make_website_email_finder, parse_int
from .export import write_outputs
from .filtering import apply_filters, summarize, to_frame
from .models import Influencer
from .youtube_client import QuotaExceededError, YouTubeAPIError, YouTubeClient

log = logging.getLogger("outreach.pipeline")


def _in_range(channel: dict, criteria: FilterCriteria) -> bool:
    stats = channel.get("statistics", {})
    if stats.get("hiddenSubscriberCount"):
        return False
    n = parse_int(stats.get("subscriberCount"))
    return n is not None and criteria.min_followers <= n <= criteria.max_followers


def collect(client: YouTubeClient, niche: Niche, criteria: FilterCriteria, *, target_micro: int = 120,
            pages_per_query: int = 3, days_back: int = 90, max_search_calls: int = 40,
            region: str | None = None, language: str | None = "en", videos_per_channel: int = 10,
            website_email_fn=None) -> tuple[list[Influencer], list[dict]]:
    """Discover channels via recent niche videos, then enrich. Stops at the target, the search
    budget, or quota exhaustion - whichever comes first - and keeps everything collected so far."""
    matcher = ThemeMatcher(niche)
    published_after = datetime.now(timezone.utc) - timedelta(days=days_back)
    seen: set[str] = set()
    records: list[Influencer] = []
    rejected: list[dict] = []
    micro = 0

    def finished() -> bool:
        return micro >= target_micro or client.search_calls >= max_search_calls

    try:
        for query in niche.search_queries:
            token = None
            for _ in range(pages_per_query):
                if finished():
                    break
                ids, token = client.search_page(query, published_after, token, region, language)
                new = [i for i in ids if i not in seen]
                seen.update(new)
                for channel in client.get_channels(new):
                    in_range = _in_range(channel, criteria)
                    videos_raw = None
                    if in_range:
                        playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                        videos_raw = []
                        if playlist:
                            try:
                                videos_raw = client.get_recent_videos(playlist, videos_per_channel)
                            except QuotaExceededError:
                                raise
                            except YouTubeAPIError as exc:
                                log.warning("No videos for %s: %s", channel.get("id"), exc)
                    try:
                        records.append(build_influencer(channel, videos_raw, niche, criteria, matcher,
                                                        website_email_fn if in_range else None))
                        micro += in_range
                    except ValidationError as exc:
                        rejected.append({"channel_id": channel.get("id"), "error": str(exc).replace("\n", " ")})
                log.info("query=%r  discovered=%d  in-range=%d  search_calls=%d",
                         query, len(records), micro, client.search_calls)
                if not token:
                    break
            if finished():
                break
    except QuotaExceededError as exc:
        log.warning("%s - stopping discovery early and keeping %d records", exc, len(records))
    return records, rejected


def run_pipeline(client: YouTubeClient, niche: Niche, criteria: FilterCriteria, out_dir: Path,
                 **options) -> dict | None:
    records, rejected = collect(client, niche, criteria, **options)
    if not records:
        log.error("No channels collected - nothing to write.")
        return None
    df = apply_filters(to_frame(records), criteria)
    summary = summarize(df)
    summary.update({
        "niche": niche.label, "source": "YouTube Data API v3 (official API)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_search_calls_made": client.search_calls, "api_calls_made": client.api_calls,
        "rejected_by_validation": len(rejected),
        "criteria": {k: v for k, v in vars(criteria).items()},
        "meets_50_discovered": summary["total_discovered"] >= 50,
        "meets_50_qualified": summary["passed"] >= 50,
    })
    summary["files"] = {k: str(v) for k, v in write_outputs(records, df, rejected, out_dir, summary).items()}
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Micro-influencer discovery, enrichment and filtering")
    p.add_argument("--niche", default="fashion_beauty", choices=sorted(NICHES))
    p.add_argument("--target-micro", type=int, default=120,
                   help="stop once this many channels inside the follower range were enriched")
    p.add_argument("--pages-per-query", type=int, default=3)
    p.add_argument("--max-search-calls", type=int, default=40,
                   help="cap on real search.list calls (default API quota is ~100/day)")
    p.add_argument("--days-back", type=int, default=90)
    p.add_argument("--region", default=None, help="ISO country code to bias search, e.g. US, IN")
    p.add_argument("--language", default="en")
    p.add_argument("--website-emails", action="store_true",
                   help="also look for public emails on creators' own websites (robots.txt respected)")
    p.add_argument("--no-cache", action="store_true", help="ignore the on-disk API cache")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        client = YouTubeClient(config.YOUTUBE_API_KEY, None if args.no_cache else config.DATA_DIR / "cache")
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2
    finder = make_website_email_finder(config.BOT_USER_AGENT) \
        if (args.website_emails or config.ENRICH_FROM_WEBSITE) else None
    summary = run_pipeline(
        client, NICHES[args.niche], FilterCriteria(), config.DATA_DIR,
        target_micro=args.target_micro, pages_per_query=args.pages_per_query,
        max_search_calls=args.max_search_calls, days_back=args.days_back, region=args.region,
        language=args.language, website_email_fn=finder)
    if summary is None:
        return 1
    print(f"\nDiscovered {summary['total_discovered']} | PASS {summary['passed']} | "
          f"FAIL {summary['failed']} | PASS with email {summary['passed_with_email']}")
    print("Fail counts:", summary["fail_counts"])
    for name, path in summary["files"].items():
        print(f"  {name}: {path}")
    if not summary["meets_50_discovered"]:
        print("WARNING: fewer than 50 channels discovered - raise --pages-per-query / --max-search-calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
