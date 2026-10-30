import pandas as pd

from outreach.config import FilterCriteria
from outreach.filtering import apply_filters, summarize, to_frame
from outreach.models import Influencer

NOW = pd.Timestamp("2026-10-29", tz="UTC")


def rec(cid, **kw):
    base = dict(channel_id=cid, name=cid, profile_url=f"https://www.youtube.com/channel/{cid}",
                niche="Fashion & Beauty", follower_count=20_000, engagement_rate=0.05,
                niche_video_share=0.8, content_themes=["Skincare"], enriched=True,
                last_upload_at=pd.Timestamp("2026-10-20", tz="UTC").to_pydatetime())
    base.update(kw)
    return Influencer(**base)


def test_pass_and_fail_reasons_are_explicit():
    records = [
        rec("good", contact_email="hi@glow.test"),
        rec("tiny", follower_count=900),
        rec("lowER", engagement_rate=0.004),
        rec("offniche", niche_video_share=0.1, content_themes=[]),
        rec("stale", last_upload_at=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime()),
        rec("unsafe", brand_safety_hits=["casino"]),
        rec("noER", engagement_rate=None),
        rec("macro", follower_count=2_500_000, enriched=False, engagement_rate=None,
            niche_video_share=None, last_upload_at=None),
    ]
    df = apply_filters(to_frame(records), FilterCriteria(), now=NOW).set_index("channel_id")
    assert df["filter_status"].to_dict() == {
        "good": "PASS", "tiny": "FAIL", "lowER": "FAIL", "offniche": "FAIL",
        "stale": "FAIL", "unsafe": "FAIL", "noER": "FAIL", "macro": "FAIL"}
    assert "FAIL followers: 900" in df.loc["tiny", "filter_reasons"]
    assert "FAIL engagement: 0.40%" in df.loc["lowER", "filter_reasons"]
    assert "FAIL niche relevance" in df.loc["offniche", "filter_reasons"]
    assert "FAIL activity" in df.loc["stale", "filter_reasons"]
    assert "blocked terms: casino" in df.loc["unsafe", "filter_reasons"]
    assert "insufficient data" in df.loc["noER", "filter_reasons"]
    assert "SKIP engagement/niche/activity" in df.loc["macro", "filter_reasons"]
    assert "PASS followers" in df.loc["good", "filter_reasons"]
    s = summarize(df.reset_index())
    assert s["passed"] == 1 and s["passed_with_email"] == 1 and s["fail_counts"]["followers"] == 2


def test_geography_rule_only_applies_when_configured():
    records = [rec("us", creator_country="US"), rec("in", creator_country="IN"), rec("unknown")]
    df = apply_filters(to_frame(records), FilterCriteria(allowed_countries=("US",)), now=NOW).set_index("channel_id")
    assert df["filter_status"].to_dict() == {"us": "PASS", "in": "FAIL", "unknown": "PASS"}
    df2 = apply_filters(to_frame(records), FilterCriteria(), now=NOW)
    assert df2["passed"].all()
