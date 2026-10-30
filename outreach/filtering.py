"""Filtering & classification with Pandas. Every record gets PASS/FAIL plus a reason per criterion."""
from __future__ import annotations

import pandas as pd

from .config import FilterCriteria
from .models import Influencer

_CHECKS = ("platform", "followers", "engagement", "niche", "activity", "brand_safety", "geography")


def to_frame(records: list[Influencer]) -> pd.DataFrame:
    rows = []
    for r in records:
        d = r.model_dump(exclude={"recent_videos"})
        d["n_videos"] = len(r.recent_videos)
        rows.append(d)
    return pd.DataFrame(rows)


def apply_filters(df: pd.DataFrame, c: FilterCriteria, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Adds chk_* booleans, days_since_upload, passed, filter_status and filter_reasons."""
    now = now or pd.Timestamp.now(tz="UTC")
    df = df.copy()
    followers = pd.to_numeric(df["follower_count"], errors="coerce")
    rate = pd.to_numeric(df["engagement_rate"], errors="coerce")
    share = pd.to_numeric(df["niche_video_share"], errors="coerce")
    df["days_since_upload"] = (now - pd.to_datetime(df["last_upload_at"], utc=True)).dt.days

    df["chk_platform"] = df["platform"].isin(c.platforms)
    df["chk_followers"] = followers.between(c.min_followers, c.max_followers)     # NaN -> False
    df["chk_engagement"] = rate >= c.min_engagement                                # NaN -> False
    df["chk_niche"] = share >= c.min_niche_share                                   # NaN -> False
    df["chk_activity"] = df["days_since_upload"] <= c.max_days_since_upload        # NaN -> False
    df["chk_brand_safety"] = df["brand_safety_hits"].map(len) == 0
    df["chk_geography"] = True if c.allowed_countries is None else (
        df["creator_country"].isna() | df["creator_country"].isin(c.allowed_countries))

    df["passed"] = df[[f"chk_{k}" for k in _CHECKS]].all(axis=1)
    df["filter_status"] = df["passed"].map({True: "PASS", False: "FAIL"})
    df["filter_reasons"] = [" | ".join(_reasons(r, c)) for r in df.itertuples(index=False)]
    return df


def _reasons(r, c: FilterCriteria) -> list[str]:
    out: list[str] = []

    def add(ok: bool, label: str, detail: str) -> None:
        out.append(f"{'PASS' if ok else 'FAIL'} {label}: {detail}")

    add(r.chk_platform, "platform", r.platform)
    f = r.follower_count
    add(r.chk_followers, "followers",
        "hidden or not available" if pd.isna(f) else f"{int(f):,} (need {c.min_followers:,}-{c.max_followers:,})")
    if not r.enriched:
        out.append("SKIP engagement/niche/activity: recent videos not fetched")
    else:
        er = r.engagement_rate
        add(r.chk_engagement, "engagement",
            f"insufficient data (need {c.min_engagement_videos}+ videos with public likes)" if pd.isna(er)
            else f"{er:.2%} (need >= {c.min_engagement:.1%})")
        sh = r.niche_video_share
        themes = ", ".join(r.content_themes) or "none"
        add(r.chk_niche, "niche relevance",
            "no videos found" if pd.isna(sh) else
            f"{sh:.0%} of recent videos match {r.niche} keywords (need >= {c.min_niche_share:.0%}); themes: {themes}")
        d = r.days_since_upload
        add(r.chk_activity, "activity",
            "no uploads found" if pd.isna(d) else f"last upload {int(d)} days ago (need <= {c.max_days_since_upload})")
    add(r.chk_brand_safety, "brand safety",
        "no blocked terms" if not r.brand_safety_hits else "blocked terms: " + ", ".join(r.brand_safety_hits))
    if c.allowed_countries:
        add(r.chk_geography, "geography", r.creator_country or "country not declared (not failing)")
    out.append(f"INFO contact email: {r.contact_email}")
    return out


def summarize(df: pd.DataFrame) -> dict:
    enriched = df["enriched"].astype(bool)
    passed = df[df["passed"]]
    return {
        "total_discovered": int(len(df)),
        "in_follower_range_and_enriched": int(enriched.sum()),
        "passed": int(len(passed)),
        "failed": int((~df["passed"]).sum()),
        "passed_with_email": int((passed["contact_email"] != "Not Found").sum()),
        "fail_counts": {
            "platform": int((~df["chk_platform"]).sum()),
            "followers": int((~df["chk_followers"]).sum()),
            "engagement": int((enriched & ~df["chk_engagement"]).sum()),
            "niche_relevance": int((enriched & ~df["chk_niche"]).sum()),
            "activity": int((enriched & ~df["chk_activity"]).sum()),
            "brand_safety": int((~df["chk_brand_safety"]).sum()),
            "geography": int((~df["chk_geography"]).sum()),
        },
    }
