"""Pydantic models. Missing data is explicit ("Not Found" / "Not Available"), never guessed."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

NOT_FOUND = "Not Found"
NOT_AVAILABLE = "Not Available"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


class RecentVideo(BaseModel):
    video_id: str
    title: str
    url: str
    published_at: datetime
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    tags: list[str] = Field(default_factory=list)


class Influencer(BaseModel):
    # --- mandatory fields (assignment section 3) ---
    channel_id: str
    name: str = Field(min_length=1)
    platform: str = "YouTube"
    profile_url: str
    follower_count: int | None = Field(default=None, ge=0)
    engagement_rate: float | None = Field(default=None, ge=0)  # fraction, e.g. 0.045 = 4.5%
    niche: str
    content_themes: list[str] = Field(default_factory=list)
    contact_email: str = NOT_FOUND
    # --- optional fields ---
    instagram_url: str | None = None
    tiktok_url: str | None = None
    website: str | None = None
    audience_age: str = NOT_AVAILABLE
    audience_gender: str = NOT_AVAILABLE
    audience_geography: str = NOT_AVAILABLE
    creator_country: str | None = None        # declared by the creator, NOT audience geography
    # --- evidence used for filtering / personalization ---
    email_source: str = NOT_FOUND
    engagement_sample_size: int = 0
    niche_video_share: float | None = None
    description: str = ""
    recent_videos: list[RecentVideo] = Field(default_factory=list)
    last_upload_at: datetime | None = None
    brand_safety_hits: list[str] = Field(default_factory=list)
    enriched: bool = False                    # False = videos not fetched (outside follower range)
    field_status: dict[str, str] = Field(default_factory=dict)  # provenance per field
    source: str = "YouTube Data API v3"
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("profile_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("profile_url must be an https URL")
        return v

    @field_validator("contact_email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        if v != NOT_FOUND and not EMAIL_RE.fullmatch(v):
            raise ValueError(f"invalid email: {v!r}")
        return v
