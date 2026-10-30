import json

import pandas as pd

from outreach.config import FASHION_BEAUTY, FilterCriteria
from outreach.pipeline import run_pipeline
from outreach.youtube_client import YouTubeClient
from tests.conftest import FakeYouTube, make_channel, make_videos


def test_end_to_end_with_fake_api(tmp_path, beauty_titles):
    channels = [
        make_channel("UC_A", "Ami Glow", 20_000, desc="Business: hello@amiglow.test IG instagram.com/ami.glow"),
        make_channel("UC_B", "Nora Style", 30_000, desc="Just vibes"),
        make_channel("UC_C", "Big Brand TV", 2_000_000),
        make_channel("UC_D", "Gamer Gus", 15_000),
        make_channel("UC_E", "Old Glow", 10_000),
        make_channel("UC_F", "Hidden Likes", 12_000),
        make_channel("UC_G", "Hidden Subs", 0, hidden=True),
    ]
    videos = {
        "UC_A": make_videos("UC_A", beauty_titles, views=1000, likes=50, comments=10),
        "UC_B": make_videos("UC_B", beauty_titles, views=2000, likes=90, comments=20),
        "UC_C": make_videos("UC_C", beauty_titles),
        "UC_D": make_videos("UC_D", ["Minecraft speedrun", "Elden Ring boss", "Valorant ranked", "Zelda", "Mario"]),
        "UC_E": make_videos("UC_E", beauty_titles, days_ago=200),
        "UC_F": make_videos("UC_F", beauty_titles, hide_likes=True),
        "UC_G": make_videos("UC_G", beauty_titles),
    }
    api = FakeYouTube(channels, videos)
    client = YouTubeClient("k", cache_dir=tmp_path / "cache", session=api, backoff=0)
    summary = run_pipeline(client, FASHION_BEAUTY, FilterCriteria(), tmp_path / "out",
                           target_micro=100, pages_per_query=1, max_search_calls=40)

    assert summary["total_discovered"] == 7 and summary["passed"] == 2 and summary["passed_with_email"] == 1
    assert summary["meets_50_discovered"] is False          # honest: only 7 in this offline test
    assert api.calls.count("playlistItems") == 5            # macro + hidden-subs channels never enriched

    all_df = pd.read_csv(tmp_path / "out" / "influencers_all.csv").set_index("Name")
    assert all_df.loc["Nora Style", "Contact Email"] == "Not Found"
    assert all_df.loc["Ami Glow", "Contact Email"] == "hello@amiglow.test"
    assert all_df.loc["Ami Glow", "Instagram"] == "https://www.instagram.com/ami.glow/"
    assert all_df.loc["Ami Glow", "Audience Age"] == "Not Available"
    assert all_df.loc["Hidden Subs", "Followers"] == "Not Available"
    assert all_df.loc["Big Brand TV", "Status"] == "FAIL" and "SKIP" in all_df.loc["Big Brand TV", "Filter Reasons"]
    assert len(pd.read_csv(tmp_path / "out" / "influencers_qualified.csv")) == 2

    data = json.loads((tmp_path / "out" / "influencers_all.json").read_text())
    ami = next(d for d in data if d["name"] == "Ami Glow")
    assert ami["filter_status"] == "PASS" and len(ami["recent_videos"]) == 5
    assert (tmp_path / "out" / "run_summary.json").exists()
