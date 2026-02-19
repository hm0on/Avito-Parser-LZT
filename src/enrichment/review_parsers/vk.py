"""VK API review parser — fetches wall posts from company groups."""

from __future__ import annotations

import re
from datetime import datetime

import httpx
import structlog

from src.collectors.base import RawReview
from src.config import settings
from src.enrichment.review_parsers.base import AbstractReviewParser

log = structlog.get_logger(__name__)

VK_API_BASE = "https://api.vk.com/method"
VK_API_VERSION = "5.199"

# Matches individual wall posts — not a group page, skip these
_WALL_POST_RE = re.compile(r"/wall-?\d+_\d+")


class VkParser(AbstractReviewParser):
    source_name = "vk"
    supported_domains = ["vk.com", "m.vk.com"]

    async def parse(self, url: str, company_name: str) -> list[RawReview]:
        if not settings.vk_access_token:
            log.debug("vk_parser.skip", reason="VK_ACCESS_TOKEN not set")
            return []

        # Skip URLs pointing to individual posts, not group pages
        if _WALL_POST_RE.search(url):
            return []

        screen_name = self._extract_screen_name(url)
        if not screen_name:
            return []

        async with httpx.AsyncClient(timeout=20.0) as client:
            group_id = await self._get_group_id(screen_name, client)
            if not group_id:
                return []
            return await self._get_wall_posts(group_id, client)

    def _extract_screen_name(self, url: str) -> str | None:
        m = re.search(r"vk\.com/([^/?#]+)", url)
        if m:
            name = m.group(1)
            # Skip numeric-only segments (e.g. vk.com/12345 is a user, not a group page)
            return name if not name.isdigit() else None
        return None

    async def _get_group_id(self, screen_name: str, client: httpx.AsyncClient) -> int | None:
        params = {
            "group_id": screen_name,
            "fields": "",
            "access_token": settings.vk_access_token,
            "v": VK_API_VERSION,
        }
        try:
            resp = await client.get(f"{VK_API_BASE}/groups.getById", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            log.warning("vk.get_group_id.error", screen_name=screen_name, error=str(exc))
            return None

        if "error" in data:
            log.debug("vk.get_group_id.api_error", error=data["error"])
            return None

        groups = data.get("response", {}).get("groups") or data.get("response", [])
        if not groups:
            return None
        return groups[0].get("id")

    async def _get_wall_posts(self, group_id: int, client: httpx.AsyncClient) -> list[RawReview]:
        params = {
            "owner_id": f"-{group_id}",
            "count": 50,
            "filter": "all",
            "access_token": settings.vk_access_token,
            "v": VK_API_VERSION,
        }
        try:
            resp = await client.get(f"{VK_API_BASE}/wall.get", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            log.warning("vk.wall_get.error", group_id=group_id, error=str(exc))
            return []

        if "error" in data:
            err = data["error"]
            # error_code 6 = too many requests per second
            if err.get("error_code") == 6:
                log.warning("vk.wall_get.rate_limit", group_id=group_id)
            return []

        items = data.get("response", {}).get("items", [])
        reviews: list[RawReview] = []
        for item in items:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            reviews.append(
                RawReview(
                    source=self.source_name,
                    text=text,
                    rating=None,  # wall posts have no star ratings
                    author=None,  # avoid extra users.get API call
                    review_date=datetime.utcfromtimestamp(item["date"]) if item.get("date") else None,
                    source_link=f"https://vk.com/wall-{group_id}_{item['id']}",
                )
            )

        log.info("vk.parse.done", group_id=group_id, count=len(reviews))
        return reviews
