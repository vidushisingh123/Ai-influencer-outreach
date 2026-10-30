"""Central configuration: environment, filter criteria and niche definitions."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
ENRICH_FROM_WEBSITE = os.getenv("ENRICH_FROM_WEBSITE", "0") == "1"
BOT_USER_AGENT = os.getenv("BOT_USER_AGENT", "EDXSO-OutreachBot/0.1 (assignment prototype)")


@dataclass(frozen=True)
class FilterCriteria:
    platforms: tuple[str, ...] = ("YouTube",)
    min_followers: int = 5_000
    max_followers: int = 100_000
    min_engagement: float = 0.02          # (likes + comments) / views, as a fraction
    min_engagement_videos: int = 3        # videos needed before an engagement rate is trusted
    min_niche_share: float = 0.4          # share of recent videos that match niche keywords
    max_days_since_upload: int = 90       # brand-fit: creator must be active
    allowed_countries: tuple[str, ...] | None = None  # ISO-2, None = no geography rule


@dataclass
class Niche:
    key: str
    label: str
    search_queries: tuple[str, ...]
    themes: dict[str, tuple[str, ...]]    # theme name -> keywords/phrases
    blocklist: tuple[str, ...] = ()       # brand-safety terms

    @property
    def all_keywords(self) -> tuple[str, ...]:
        return tuple(k for kws in self.themes.values() for k in kws)


FASHION_BEAUTY = Niche(
    key="fashion_beauty",
    label="Fashion & Beauty",
    search_queries=(
        "skincare routine", "everyday makeup tutorial", "get ready with me",
        "fashion haul", "outfit ideas", "hair care routine", "thrift haul fashion",
        "capsule wardrobe", "drugstore makeup review", "styling tips",
        "nail art tutorial", "perfume collection",
    ),
    themes={
        "Skincare": ("skincare", "skin care", "serum", "moisturizer", "sunscreen", "cleanser", "acne"),
        "Makeup": ("makeup", "make up", "foundation", "lipstick", "eyeshadow", "mascara",
                   "concealer", "blush", "grwm", "get ready with me"),
        "Haircare & styling": ("haircare", "hair care", "hairstyle", "hair routine", "hair", "curls"),
        "Fragrance": ("perfume", "fragrance", "cologne"),
        "Nails": ("nails", "nail art", "manicure"),
        "Fashion & outfits": ("fashion", "outfit", "outfits", "ootd", "lookbook", "wardrobe",
                              "styling", "style tips", "streetwear"),
        "Hauls & thrifting": ("haul", "thrift", "thrifting", "try on", "try-on"),
        "Sustainable fashion": ("sustainable fashion", "slow fashion", "secondhand", "second hand"),
        "Accessories & jewelry": ("jewelry", "jewellery", "handbag", "accessories"),
    },
    blocklist=("onlyfans", "nsfw", "18+", "casino", "betting", "gambling", "forex signals"),
)

NICHES: dict[str, Niche] = {FASHION_BEAUTY.key: FASHION_BEAUTY}

# ---- Phase 2: sender identity (never invented by the model) ----
BRAND_NAME = os.getenv("BRAND_NAME", "").strip()
SENDER_NAME = os.getenv("SENDER_NAME", "").strip()
BRAND_DESCRIPTION = os.getenv("BRAND_DESCRIPTION", "").strip()
COLLAB_TYPE = os.getenv("COLLAB_TYPE", "").strip() or "UGC content creation"

# ---- Phase 3: storage, sending, API/dashboard ----
DB_PATH = Path(os.getenv("DB_PATH", "").strip() or DATA_DIR / "outreach.db")
EMAIL_MODE = os.getenv("EMAIL_MODE", "simulation").strip().lower() or "simulation"   # "simulation" | "smtp"
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
EMAIL_FOOTER = os.getenv("EMAIL_FOOTER", "").strip()   # e.g. opt-out line + postal address, appended on real sends
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").strip()
try:
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
except ValueError:
    SMTP_PORT = 587

# ---- Phase 3: storage, API, sending (simulation is the default; nothing is sent unless EMAIL_MODE=smtp) ----
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


DB_PATH = Path(os.getenv("DB_PATH") or DATA_DIR / "outreach.db")
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")
EMAIL_MODE = (os.getenv("EMAIL_MODE", "simulation").strip().lower() or "simulation")
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = _int_env("SMTP_PORT", 587)
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
EMAIL_FOOTER = os.getenv("EMAIL_FOOTER", "").replace("\\n", "\n")
