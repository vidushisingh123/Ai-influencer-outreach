import pytest
from pydantic import ValidationError

from outreach.config import FASHION_BEAUTY, FilterCriteria
from outreach.enrichment import (ThemeMatcher, build_influencer, compute_engagement, extract_emails,
                                 extract_links, extract_themes, make_website_email_finder, parse_videos)
from outreach.models import NOT_FOUND
from tests.conftest import FakeResp, make_channel, make_videos

C = FilterCriteria()
M = ThemeMatcher(FASHION_BEAUTY)


def test_extract_emails_only_returns_literal_public_emails():
    text = "Business: Hello@GlowStudio.test. Logo logo@2x.png sample@example.com"
    assert extract_emails(text) == ["hello@glowstudio.test"]
    assert extract_emails("DM me for collabs, no email here") == []


def test_extract_links():
    text = "IG instagram.com/glow.with.ami TikTok https://www.tiktok.com/@ami.tt shop https://amzn.to/x site https://amiglow.test/home."
    links = extract_links(text)
    assert links["instagram_url"] == "https://www.instagram.com/glow.with.ami/"
    assert links["tiktok_url"] == "https://www.tiktok.com/@ami.tt"
    assert links["website"] == "https://amiglow.test/home"
    assert extract_links("instagram.com/reel/abc")["instagram_url"] is None


def test_engagement_skips_hidden_likes_and_needs_min_videos(beauty_titles):
    ok = parse_videos(make_videos("a", beauty_titles, views=1000, likes=40, comments=10))
    rate, used = compute_engagement(ok, 3)
    assert used == 5 and rate == pytest.approx(0.05)
    hidden = parse_videos(make_videos("a", beauty_titles, hide_likes=True))
    assert compute_engagement(hidden, 3) == (None, 0)
    assert compute_engagement(ok[:2], 3)[0] is None


def test_themes_need_repeated_evidence_and_niche_share(beauty_titles):
    titles = beauty_titles + ["Winter skincare serum haul", "Minecraft speedrun"]
    themes, share = extract_themes(parse_videos(make_videos("a", titles)), "", M)
    assert {"Skincare", "Makeup"} <= set(themes)            # each backed by 2+ videos
    assert "Haircare & styling" not in themes               # only one video mentions hair
    assert share == pytest.approx(6 / 7)
    assert extract_themes([], "", M) == ([], None)


def test_missing_email_is_not_found_and_hidden_subs_are_none(beauty_titles):
    ch = make_channel("UC1", "Ami Glow", 12000, desc="No contact info")
    rec = build_influencer(ch, make_videos("UC1", beauty_titles), FASHION_BEAUTY, C, M)
    assert rec.contact_email == NOT_FOUND and rec.field_status["contact_email"] == "not_found"
    hidden = build_influencer(make_channel("UC2", "X", 0, hidden=True), None, FASHION_BEAUTY, C, M)
    assert hidden.follower_count is None and hidden.field_status["follower_count"] == "hidden_by_creator"
    assert hidden.engagement_rate is None and hidden.field_status["engagement_rate"].startswith("not_collected")
    assert hidden.audience_age == "Not Available"


def test_blank_name_is_rejected_by_validation():
    with pytest.raises(ValidationError):
        build_influencer(make_channel("UC3", "  ", 9000), None, FASHION_BEAUTY, C, M)


def test_website_finder_respects_robots_and_finds_email():
    class Site:
        def __init__(self, robots):
            self.robots, self.fetched = robots, []

        def get(self, url, headers=None, timeout=None):
            self.fetched.append(url)
            if url.endswith("/robots.txt"):
                return FakeResp(200, text=self.robots)
            return FakeResp(200, text='<a href="mailto:pr@amiglow.test">mail</a>', headers={"Content-Type": "text/html"})

    allowed = Site("User-agent: *\nAllow: /")
    assert make_website_email_finder("TestBot/1.0", allowed)("https://amiglow.test/home") == "pr@amiglow.test"
    blocked = Site("User-agent: *\nDisallow: /")
    assert make_website_email_finder("TestBot/1.0", blocked)("https://amiglow.test") is None
    assert blocked.fetched == ["https://amiglow.test/robots.txt"]   # no page fetched when disallowed
