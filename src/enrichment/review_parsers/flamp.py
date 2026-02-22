"""Flamp.ru review parser — JSON API with HTML scrape fallback."""

from __future__ import annotations

import re
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.collectors.base import RawReview
from src.enrichment.review_parsers.base import AbstractReviewParser

log = structlog.get_logger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "application/json, text/html, */*",
}

REVIEWS_API = "https://omsk.flamp.ru/api/2.0/filials/{filial_id}/reviews/"


class FlampParser(AbstractReviewParser):
    source_name = "flamp"
    supported_domains = ["flamp.ru"]

    async def parse(self, url: str, company_name: str) -> list[RawReview]:
        filial_id = self._extract_filial_id(url)
        if not filial_id:
            log.debug("flamp.no_id", url=url)
            return []

        from src.proxy import proxy_manager
        proxy_url = proxy_manager.get_next()

        async with httpx.AsyncClient(
            timeout=15.0,
            headers=_BROWSER_HEADERS,
            proxy=proxy_url or None,
            follow_redirects=True,
        ) as client:
            # Primary: JSON API
            try:
                reviews = await self._fetch_via_api(filial_id, client)
                if reviews:
                    log.info("flamp.api.done", filial_id=filial_id, count=len(reviews))
                    return reviews
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    log.info("flamp.api.blocked", filial_id=filial_id, fallback="scrape")
                else:
                    log.warning("flamp.api.error", filial_id=filial_id, status=exc.response.status_code)
            except Exception as exc:
                log.warning("flamp.api.network_error", filial_id=filial_id, error=repr(exc))

            # Fallback: HTML scrape
            reviews = await self._fetch_via_scrape(url, client)
            log.info("flamp.scrape.done", url=url, count=len(reviews))
            return reviews

    def _extract_filial_id(self, url: str) -> str | None:
        m = re.search(r"-(\d+)/?$", url.rstrip("/"))
        return m.group(1) if m else None

    async def _fetch_via_api(
        self, filial_id: str, client: httpx.AsyncClient
    ) -> list[RawReview]:
        resp = await client.get(
            REVIEWS_API.format(filial_id=filial_id),
            params={"limit": 50, "offset": 0},
        )
        resp.raise_for_status()
        data = resp.json()

        reviews: list[RawReview] = []
        for item in data.get("results") or data.get("items") or []:
            text = (item.get("text") or "").strip()
            rating_raw = item.get("rating")
            created_raw = item.get("created_at") or item.get("date_created")
            author = (item.get("user") or {}).get("name")

            reviews.append(
                RawReview(
                    source=self.source_name,
                    text=text or None,
                    rating=float(rating_raw) if rating_raw is not None else None,
                    author=author,
                    review_date=_parse_date(created_raw),
                    source_link=f"https://omsk.flamp.ru/firm/-{filial_id}",
                )
            )
        return reviews

    async def _fetch_via_scrape(
        self, url: str, client: httpx.AsyncClient
    ) -> list[RawReview]:
        try:
            resp = await client.get(url)
            if resp.status_code in (403, 404, 429):
                log.info("flamp.scrape.blocked", status=resp.status_code, url=url)
                return []
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("flamp.scrape.error", url=url, error=repr(exc))
            return []

        return self._parse_page(resp.text, url)

    def _parse_page(self, html: str, page_url: str) -> list[RawReview]:
        soup = BeautifulSoup(html, "lxml")
        reviews: list[RawReview] = []

        for block in soup.select("div.review-item, article.review, [class*='review-item']")[:20]:
            # Text
            text_el = block.select_one(
                "div.review-item__text, div[itemprop='reviewBody'], [class*='review-text']"
            )
            text = text_el.get_text(strip=True) if text_el else None

            # Rating
            rating: float | None = None
            rating_el = block.select_one(
                "meta[itemprop='ratingValue'], span.stars__value, [class*='rating-value']"
            )
            if rating_el:
                rating_str = rating_el.get("content") or rating_el.get_text(strip=True)
                try:
                    rating = float(rating_str.replace(",", "."))
                except (ValueError, AttributeError):
                    pass

            # Author
            author_el = block.select_one(
                "span[itemprop='author'], div.review-item__author, [class*='author']"
            )
            author = author_el.get_text(strip=True) if author_el else None

            # Date
            date_el = block.select_one("time[itemprop='datePublished'], time[datetime]")
            review_date: datetime | None = None
            if date_el:
                dt_attr = date_el.get("datetime") or date_el.get_text(strip=True)
                review_date = _parse_date(dt_attr)

            if text:
                reviews.append(
                    RawReview(
                        source=self.source_name,
                        text=text,
                        rating=rating,
                        author=author,
                        review_date=review_date,
                        source_link=page_url,
                    )
                )

        return reviews


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return dateutil_parser.parse(raw)
    except Exception:
        return None
