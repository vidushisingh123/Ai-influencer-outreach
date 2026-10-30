"""Thin YouTube Data API v3 client: retries, on-disk response cache, explicit quota errors."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)
BASE = "https://www.googleapis.com/youtube/v3"
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


class YouTubeAPIError(RuntimeError):
    """Any non-recoverable API failure."""


class QuotaExceededError(YouTubeAPIError):
    """Daily quota exhausted; callers should stop and keep what they already collected."""


class YouTubeClient:
    def __init__(self, api_key: str, cache_dir: Path | None = None, session=None,
                 max_retries: int = 3, backoff: float = 1.0, timeout: int = 15):
        if not api_key:
            raise ValueError("YOUTUBE_API_KEY is not set (see .env.example)")
        self.api_key = api_key
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = session or requests.Session()
        self.max_retries, self.backoff, self.timeout = max_retries, backoff, timeout
        self.search_calls = 0   # real network search calls (cache hits are free)
        self.api_calls = 0      # real network calls of any kind
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------- low level ----------
    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "***")

    def _cache_file(self, endpoint: str, params: dict) -> Path | None:
        if not self.cache_dir:
            return None
        raw = json.dumps([endpoint, sorted(params.items())], default=str)
        return self.cache_dir / f"{hashlib.sha256(raw.encode()).hexdigest()[:24]}.json"

    def _get(self, endpoint: str, params: dict) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        cache_file = self._cache_file(endpoint, params)
        if cache_file and cache_file.exists():
            try:
                return json.loads(cache_file.read_text())
            except (OSError, ValueError):
                pass  # corrupt cache entry -> refetch

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                resp = self.session.get(f"{BASE}/{endpoint}", params={**params, "key": self.api_key},
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = self._redact(str(exc))
                continue
            self.api_calls += 1
            if endpoint == "search":
                self.search_calls += 1
            if resp.status_code == 200:
                data = resp.json()
                if cache_file:
                    cache_file.write_text(json.dumps(data))
                return data
            reason = _error_reason(resp)
            if resp.status_code == 403 and reason in _QUOTA_REASONS:
                raise QuotaExceededError(f"YouTube quota exhausted ({reason}) on {endpoint}")
            last_error = f"HTTP {resp.status_code} {reason}"
            if resp.status_code == 429 or resp.status_code >= 500 or reason == "rateLimitExceeded":
                continue  # transient -> retry with backoff
            raise YouTubeAPIError(f"{endpoint} failed: {last_error}")
        raise YouTubeAPIError(f"{endpoint} failed after retries: {last_error}")

    # ---------- endpoints ----------
    def search_page(self, query: str, published_after: datetime, page_token: str | None = None,
                    region_code: str | None = None, language: str | None = None
                    ) -> tuple[list[str], str | None]:
        """One page (<=50) of recent videos for a query -> (unique channel ids, next page token)."""
        data = self._get("search", {
            "part": "snippet", "type": "video", "q": query, "maxResults": 50,
            "order": "relevance", "publishedAfter": published_after.strftime("%Y-%m-%dT00:00:00Z"),
            "pageToken": page_token, "regionCode": region_code, "relevanceLanguage": language,
        })
        ids = []
        for item in data.get("items", []):
            cid = item.get("snippet", {}).get("channelId")
            if cid and cid not in ids:
                ids.append(cid)
        return ids, data.get("nextPageToken")

    def get_channels(self, channel_ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(channel_ids), 50):
            data = self._get("channels", {
                "part": "snippet,statistics,contentDetails,brandingSettings",
                "id": ",".join(channel_ids[i:i + 50]), "maxResults": 50,
            })
            out.extend(data.get("items", []))
        return out

    def get_recent_videos(self, uploads_playlist_id: str, n: int = 10) -> list[dict]:
        """Latest n uploads with statistics (2 cheap calls: playlistItems + videos)."""
        pl = self._get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist_id,
                                         "maxResults": n})
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])
               if i.get("contentDetails", {}).get("videoId")]
        if not ids:
            return []
        return self._get("videos", {"part": "snippet,statistics", "id": ",".join(ids)}).get("items", [])


def _error_reason(resp) -> str:
    try:
        return resp.json()["error"]["errors"][0]["reason"]
    except Exception:  # noqa: BLE001 - best-effort parse of an error body
        return "unknown"
