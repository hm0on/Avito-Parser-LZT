"""Otzovik.com review parser — HTML scrape (first page only)."""

from __future__ import annotations

import re
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup

from src.collectors.base import RawReview
from src.enrichment.review_parsers.base import AbstractReviewParser

log = structlog.get_logger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Russian month names → month number (handles both full and abbreviated forms)
_RU_MONTHS: dict[str, int] = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4,
    "май": 5, "июн": 6, "июл": 7, "авг": 8,
    "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


class OtzovikParser(AbstractReviewParser):
    source_name = "otzovik"
    supported_domains = ["otzovik.com"]

    async def parse(self, url: str, company_name: str) -> list[RawReview]:
        async with httpx.AsyncClient(timeout=20.0, headers=_BROWSER_HEADERS) as client:
            try:
                resp = await client.get(url, follow_redirects=True)
            except httpx.HTTPError as exc:
                log.warning("otzovik.fetch.error", url=url, error=str(exc))
                return []

            if resp.status_code == 403:
                # Cloudflare protection — normal, not an error
                log.info("otzovik.cloudflare_blocked", url=url)
                return []
            if resp.status_code != 200:
                log.debug("otzovik.non_200", url=url, status=resp.status_code)
                return []

        reviews = self._parse_page(resp.text, url)
        log.info("otzovik.parse.done", url=url, count=len(reviews))
        return reviews

    def _parse_page(self, html: str, page_url: str) -> list[RawReview]:
        soup = BeautifulSoup(html, "lxml")
        reviews: list[RawReview] = []

        for block in soup.select("div.review-body, div[class*='review-item']")[:20]:
            # Text — main content
            text_el = block.select_one(
                "div.review-text-container, div[class*='review-text'], div.review_text"
            )
            if not text_el:
                text_el = block.select_one("div.description")
            text = text_el.get_text(strip=True) if text_el else None

            # Rating — numeric value or star count
            rating: float | None = None
            rating_el = block.select_one(
                "span.product-rating.user-rating, "
                "span[class*='user-rating'], "
                "input[name='product_rating']"
            )
            if rating_el:
                val = rating_el.get("value") or rating_el.get_text(strip=True)
                try:
                    rating = float(str(val).replace(",", "."))
                except (ValueError, TypeError):
                    pass

            # Author
            author_el = block.select_one("span.user-name > a, a[class*='user-login']")
            author = author_el.get_text(strip=True) if author_el else None

            # Date — "19 февраля 2026" in abbr title attribute, or span text
            review_date: datetime | None = None
            date_el = block.select_one(
                "span.review-postdate > abbr, "
                "span[class*='review-date'], "
                "abbr[class*='date']"
            )
            if date_el:
                date_str = date_el.get("title") or date_el.get_text(strip=True)
                review_date = _parse_ru_date(date_str)

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


def _parse_ru_date(text: str) -> datetime | None:
    """Parse Russian date strings like '19 февраля 2026' or '19 фев. 2026'."""
    text = text.strip().lower()
    # Remove dots from abbreviated months: "фев." → "фев"
    text = text.replace(".", "")
    parts = text.split()
    if len(parts) != 3:
        return None
    day_str, month_raw, year_str = parts
    month = _RU_MONTHS.get(month_raw)
    if not month:
        return None
    try:
        return datetime(int(year_str), month, int(day_str))
    except ValueError:
        return None
