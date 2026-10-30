"""Write the dataset files. Missing values are shown explicitly, never blank or invented."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .models import NOT_AVAILABLE, NOT_FOUND, Influencer


def _na(value, fn=lambda v: v):
    return NOT_AVAILABLE if pd.isna(value) else fn(value)


def display_frame(df: pd.DataFrame) -> pd.DataFrame:
    last = pd.to_datetime(df["last_upload_at"], utc=True).dt.strftime("%Y-%m-%d")
    return pd.DataFrame({
        "Name": df["name"], "Platform": df["platform"], "Profile URL": df["profile_url"],
        "Followers": df["follower_count"].map(lambda v: _na(v, int)),
        "Engagement Rate (%)": df["engagement_rate"].map(lambda v: _na(v, lambda x: round(x * 100, 2))),
        "Niche": df["niche"],
        "Content Themes": df["content_themes"].map(lambda t: "; ".join(t) if t else NOT_FOUND),
        "Contact Email": df["contact_email"], "Email Source": df["email_source"],
        "Instagram": df["instagram_url"].map(lambda v: v or NOT_FOUND),
        "TikTok": df["tiktok_url"].map(lambda v: v or NOT_FOUND),
        "Website": df["website"].map(lambda v: v or NOT_FOUND),
        "Audience Age": df["audience_age"], "Audience Gender": df["audience_gender"],
        "Audience Geography": df["audience_geography"],
        "Creator Country (declared)": df["creator_country"].map(lambda v: v or NOT_AVAILABLE),
        "Last Upload": last.fillna(NOT_AVAILABLE),
        "Niche Video Share (%)": df["niche_video_share"].map(lambda v: _na(v, lambda x: round(x * 100))),
        "Status": df["filter_status"], "Filter Reasons": df["filter_reasons"],
        "Data Provenance": df["field_status"].map(json.dumps),
        "Source": df["source"], "Collected At": df["collected_at"].map(lambda d: d.isoformat()),
    })


def write_outputs(records: list[Influencer], df: pd.DataFrame, rejected: list[dict],
                  out_dir: Path, summary: dict) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    shown = display_frame(df)
    paths = {
        "all_csv": out_dir / "influencers_all.csv",
        "qualified_csv": out_dir / "influencers_qualified.csv",
        "json": out_dir / "influencers_all.json",
        "summary": out_dir / "run_summary.json",
    }
    shown.to_csv(paths["all_csv"], index=False)
    shown[shown["Status"] == "PASS"].to_csv(paths["qualified_csv"], index=False)

    full = []
    for rec, row in zip(records, df.itertuples(index=False)):
        item = rec.model_dump(mode="json")
        item["filter_status"] = row.filter_status
        item["filter_reasons"] = row.filter_reasons.split(" | ")
        full.append(item)
    paths["json"].write_text(json.dumps(full, indent=2, ensure_ascii=False))
    paths["summary"].write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    if rejected:
        paths["rejected"] = out_dir / "rejected_rows.csv"
        pd.DataFrame(rejected).to_csv(paths["rejected"], index=False)
    return paths
