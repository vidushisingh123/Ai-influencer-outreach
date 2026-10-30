"""Turn raw API payloads into validated Influencer records. Nothing here guesses or invents data."""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urlparse

import requests

from .config import FilterCriteria, Niche
from .models import EMAIL_RE, NOT_AVAILABLE, NOT_FOUND, Influencer, RecentVideo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- parsing helpers
def parse_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_videos(raw_items: list[dict]) -> list[RecentVideo]:
    videos = []
    for it in raw_items:
        try:
            sn, st = it["snippet"], it.get("statistics", {})
            videos.append(RecentVideo(
                video_id=it["id"], title=sn["title"], url=f"https://www.youtube.com/watch?v={it['id']}",
                published_at=parse_dt(sn["publishedAt"]), view_count=parse_int(st.get("viewCount")),
                like_count=parse_int(st.get("likeCount")), comment_count=parse_int(st.get("commentCount")),
                tags=sn.get("tags", []),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("Skipping malformed video payload: %s", exc)
    return sorted(videos, key=lambda v: v.published_at, reverse=True)


# ---------------------------------------------------------------- contact + links
_JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_PLACEHOLDER_DOMAINS = {"example.com", "domain.com", "email.com", "yourdomain.com"}


def extract_emails(text: str) -> list[str]:
    """Emails literally present in public text. Never constructs or guesses an address."""
    found: list[str] = []
    for match in EMAIL_RE.findall(text or ""):
        email = match.strip(".").lower()
        domain = email.rsplit("@", 1)[1]
        if email.endswith(_JUNK_SUFFIXES) or domain in _PLACEHOLDER_DOMAINS or "sentry" in domain:
            continue
        if email not in found:
            found.append(email)
    return found


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)", re.I)
_TT_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9._]+)", re.I)
_IG_RESERVED = {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "about"}
_NOT_A_WEBSITE = (
    "instagram.com", "tiktok.com", "facebook.com", "fb.com", "twitter.com", "x.com", "youtube.com",
    "youtu.be", "pinterest.com", "snapchat.com", "twitch.tv", "discord.gg", "discord.com", "amzn.to",
    "amazon.com", "bit.ly", "geni.us", "tinyurl.com", "ow.ly", "shopltk.com", "liketk.it", "rstyle.me",
    "shopmy.us", "go.magik.ly", "t.co", "wa.me",
)


def extract_links(text: str) -> dict[str, str | None]:
    text = text or ""
    ig = next((h.rstrip(".") for h in _IG_RE.findall(text) if h.lower() not in _IG_RESERVED and h.rstrip(".")), None)
    tt = next((h.rstrip(".") for h in _TT_RE.findall(text) if h.rstrip(".")), None)
    website = None
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:!?")
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if host and not any(host == d or host.endswith("." + d) for d in _NOT_A_WEBSITE):
            website = url
            break
    return {
        "instagram_url": f"https://www.instagram.com/{ig}/" if ig else None,
        "tiktok_url": f"https://www.tiktok.com/@{tt}" if tt else None,
        "website": website,
    }


def make_website_email_finder(user_agent: str, session=None, timeout: int = 8):
    """Optional: look for a public email on the creator's own site (homepage + /contact, robots.txt respected)."""
    session = session or requests.Session()
    headers, agent, cache = {"User-Agent": user_agent}, user_agent.split("/")[0], {}

    def find(site_url: str) -> str | None:
        parsed = urlparse(site_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in cache:
            return cache[origin]
        email = None
        try:
            rp = robotparser.RobotFileParser()
            robots = session.get(origin + "/robots.txt", headers=headers, timeout=timeout)
            if robots.status_code in (401, 403):
                cache[origin] = None
                return None
            rp.parse(robots.text.splitlines() if robots.status_code == 200 else [])
            for path in ("/", "/contact"):
                if not rp.can_fetch(agent, origin + path):
                    continue
                page = session.get(origin + path, headers=headers, timeout=timeout)
                if page.status_code == 200 and "text/html" in page.headers.get("Content-Type", ""):
                    found = extract_emails(page.text[:500_000])
                    if found:
                        email = found[0]
                        break
        except requests.RequestException as exc:
            log.info("Website email lookup failed for %s: %s", origin, exc)
        cache[origin] = email
        return email

    return find


# ---------------------------------------------------------------- niche + engagement
def _compile(terms) -> re.Pattern | None:
    terms = sorted(set(terms), key=len, reverse=True)
    if not terms:
        return None
    return re.compile("|".join(rf"(?<!\w){re.escape(t)}(?!\w)" for t in terms), re.I)


class ThemeMatcher:
    def __init__(self, niche: Niche):
        self._themes = {name: _compile(kws) for name, kws in niche.themes.items()}
        self._block = _compile(niche.blocklist)

    def themes_in(self, text: str) -> set[str]:
        return {name for name, pat in self._themes.items() if pat.search(text or "")}

    def blocklist_hits(self, text: str) -> list[str]:
        return sorted({m.group(0).lower() for m in self._block.finditer(text or "")}) if self._block else []


def video_text(v: RecentVideo) -> str:
    return f"{v.title} {' '.join(v.tags)}"


def compute_engagement(videos: list[RecentVideo], min_videos: int) -> tuple[float | None, int]:
    """(likes + comments) / views over recent videos. Videos with hidden likes or zero views are skipped."""
    interactions = views = used = 0
    for v in videos:
        if not v.view_count or v.like_count is None:
            continue
        interactions += v.like_count + (v.comment_count or 0)
        views += v.view_count
        used += 1
    if used < min_videos or views == 0:
        return None, used
    return interactions / views, used


def extract_themes(videos: list[RecentVideo], channel_keywords: str, matcher: ThemeMatcher
                   ) -> tuple[list[str], float | None]:
    """Themes backed by evidence in recent video titles/tags + share of videos that match the niche."""
    if not videos:
        return [], None
    counts: Counter = Counter()
    relevant = 0
    for v in videos:
        found = matcher.themes_in(video_text(v))
        relevant += bool(found)
        counts.update(found)
    counts.update(matcher.themes_in(channel_keywords))  # creator-declared keywords = one extra vote
    threshold = 1 if len(videos) < 4 else 2
    return [t for t, c in counts.most_common() if c >= threshold], relevant / len(videos)


# ---------------------------------------------------------------- record builder
def build_influencer(channel: dict, videos_raw: list[dict] | None, niche: Niche, criteria: FilterCriteria,
                     matcher: ThemeMatcher, website_email_fn=None) -> Influencer:
    """videos_raw=None means 'not fetched' (e.g. outside the follower range)."""
    sn, stats = channel.get("snippet", {}), channel.get("statistics", {})
    cid, desc = channel["id"], sn.get("description", "")
    hidden = bool(stats.get("hiddenSubscriberCount"))
    followers = None if hidden else parse_int(stats.get("subscriberCount"))
    custom = sn.get("customUrl", "")
    profile_url = f"https://www.youtube.com/{custom}" if custom.startswith("@") \
        else f"https://www.youtube.com/channel/{cid}"

    links = extract_links(desc)
    emails = extract_emails(desc)
    email, email_src = (emails[0], "public_channel_description") if emails else (NOT_FOUND, NOT_FOUND)
    if not emails and website_email_fn and links["website"]:
        found = website_email_fn(links["website"])
        if found:
            email, email_src = found, "public_website_contact_page"

    enriched = videos_raw is not None
    videos = parse_videos(videos_raw or [])
    rate, n_used = compute_engagement(videos, criteria.min_engagement_videos)
    keywords = channel.get("brandingSettings", {}).get("channel", {}).get("keywords", "")
    themes, share = extract_themes(videos, keywords, matcher)
    hits = matcher.blocklist_hits(" ".join([sn.get("title", ""), desc] + [v.title for v in videos]))

    not_collected = "not_collected_outside_follower_range"
    status = {
        "follower_count": "hidden_by_creator" if hidden else "api_verified",
        "engagement_rate": f"computed_from_api_last_{n_used}_videos" if rate is not None
        else ("insufficient_data" if enriched else not_collected),
        "content_themes": "keyword_match_recent_videos" if themes
        else ("no_match" if enriched else not_collected),
        "contact_email": email_src if emails or email_src != NOT_FOUND else "not_found",
        "audience_age": "not_available_via_public_api",
        "audience_gender": "not_available_via_public_api",
        "audience_geography": "not_available_via_public_api",
        "creator_country": "declared_by_creator" if sn.get("country") else "not_declared",
    }
    return Influencer(
        channel_id=cid, name=sn.get("title", "").strip(), profile_url=profile_url,
        follower_count=followers, engagement_rate=rate, engagement_sample_size=n_used,
        niche=niche.label, content_themes=themes, niche_video_share=share,
        contact_email=email, email_source=email_src, creator_country=sn.get("country"),
        instagram_url=links["instagram_url"], tiktok_url=links["tiktok_url"], website=links["website"],
        description=desc[:1000], recent_videos=videos,
        last_upload_at=videos[0].published_at if videos else None,
        brand_safety_hits=hits, enriched=enriched, field_status=status,
    )
