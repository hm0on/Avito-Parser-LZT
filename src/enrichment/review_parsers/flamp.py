"""Flamp review parser with proxy-only HTTP access."""

from __future__ import annotations

import re
from datetime import datetime
import json
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.collectors.base import RawReview
from src.config import settings
from src.enrichment.review_parsers.base import AbstractReviewParser
from src.proxy import enrichment_proxy_manager, iter_proxy_urls

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
_FLAMP_BASE_URL = "https://omsk.flamp.ru"


class FlampParser(AbstractReviewParser):
    source_name = "flamp"
    supported_domains = ["flamp.ru"]

    async def parse(self, url: str, company_name: str) -> list[RawReview]:
        normalized_url = normalize_flamp_url(url) or url
        filial_id = self._extract_filial_id(normalized_url)
        if not filial_id:
            log.debug("flamp.no_id", url=normalized_url)
            return []

        max_reviews = max(1, int(settings.flamp_max_reviews_per_company))
        timeout = max(5.0, float(settings.flamp_timeout_seconds))
        max_attempts = max(1, int(settings.flamp_max_attempts))

        proxy_candidates: list[str | None] = []
        if enrichment_proxy_manager.count > 0:
            proxy_candidates.extend(
                iter_proxy_urls(
                    enrichment_proxy_manager,
                    purpose="flamp parser",
                    max_attempts=max_attempts,
                )
            )
        # Always do at least one direct attempt, even if proxy pool is empty or exhausted.
        proxy_candidates.append(None)

        for proxy_url in proxy_candidates:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers=_BROWSER_HEADERS,
                proxy=proxy_url,
                follow_redirects=True,
            ) as client:
                try:
                    reviews = await self._fetch_via_api(
                        filial_id,
                        client,
                        max_reviews=max_reviews,
                    )
                    if reviews:
                        log.info(
                            "flamp.api.done",
                            filial_id=filial_id,
                            reviews_collected=len(reviews),
                            proxy=proxy_url or "direct",
                        )
                        return reviews[:max_reviews]
                    log.info(
                        "flamp.api.empty",
                        filial_id=filial_id,
                        proxy=proxy_url or "direct",
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (403, 404):
                        log.info(
                            "flamp.api.blocked",
                            filial_id=filial_id,
                            fallback="scrape",
                            blocked_reason=f"http_{exc.response.status_code}",
                            proxy=proxy_url or "direct",
                        )
                    else:
                        log.warning(
                            "flamp.api.error",
                            filial_id=filial_id,
                            status=exc.response.status_code,
                            proxy=proxy_url or "direct",
                        )
                except Exception as exc:
                    log.warning(
                        "flamp.api.network_error",
                        filial_id=filial_id,
                        error=repr(exc),
                        proxy=proxy_url or "direct",
                    )

                reviews = await self._fetch_via_scrape(url, client, max_reviews=max_reviews)
                if reviews:
                    log.info(
                        "flamp.scrape.done",
                        url=normalized_url,
                        fallback_used="scrape",
                        reviews_collected=len(reviews),
                        proxy=proxy_url or "direct",
                    )
                    return reviews[:max_reviews]
                log.info(
                    "flamp.scrape.empty",
                    url=normalized_url,
                    proxy=proxy_url or "direct",
                )

        return []

    def _extract_filial_id(self, url: str) -> str | None:
        normalized_url = normalize_flamp_url(url) or url
        path = urlparse(normalized_url).path.rstrip("/")
        match = re.search(r"/firm/(?:[^/?#]*-)?(\d+)$", path)
        return match.group(1) if match else None

    async def _fetch_via_api(
        self,
        filial_id: str,
        client: httpx.AsyncClient,
        *,
        max_reviews: int,
    ) -> list[RawReview]:
        limit = min(50, max(1, max_reviews))
        reviews: list[RawReview] = []
        offset = 0
        while len(reviews) < max_reviews:
            response = await client.get(
                REVIEWS_API.format(filial_id=filial_id),
                params={"limit": limit, "offset": offset},
            )
            response.raise_for_status()
            data = response.json()

            page_items = data.get("results") or data.get("items") or []
            if not page_items:
                break

            for item in page_items:
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
                        source_link=_flamp_firm_url(filial_id),
                    )
                )
                if len(reviews) >= max_reviews:
                    break

            offset += limit

        return reviews

    async def _fetch_via_scrape(
        self,
        url: str,
        client: httpx.AsyncClient,
        *,
        max_reviews: int,
    ) -> list[RawReview]:
        normalized_url = normalize_flamp_url(url) or url
        try:
            response = await client.get(normalized_url)
            if response.status_code in (403, 404, 429):
                log.info(
                    "flamp.scrape.blocked",
                    status=response.status_code,
                    blocked_reason=f"http_{response.status_code}",
                    url=normalized_url,
                )
                return []
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("flamp.scrape.error", url=normalized_url, error=repr(exc))
            return []

        return self._parse_page(response.text, normalized_url)[:max_reviews]

    def _parse_page(self, html: str, page_url: str) -> list[RawReview]:
        soup = BeautifulSoup(html, "lxml")
        reviews: list[RawReview] = []

        for block in soup.select("div.review-item, article.review, [class*='review-item']")[:20]:
            text_el = block.select_one(
                "div.review-item__text, div[itemprop='reviewBody'], [class*='review-text']"
            )
            text = text_el.get_text(strip=True) if text_el else None

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

            author_el = block.select_one(
                "span[itemprop='author'], div.review-item__author, [class*='author']"
            )
            author = author_el.get_text(strip=True) if author_el else None

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

        if reviews:
            return _deduplicate_reviews(reviews)

        jsonld_reviews = _parse_jsonld_reviews(soup, page_url, self.source_name)
        if jsonld_reviews:
            return _deduplicate_reviews(jsonld_reviews)

        return reviews


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return dateutil_parser.parse(raw)
    except Exception:
        return None


def _flamp_firm_url(filial_id: str) -> str:
    return f"https://omsk.flamp.ru/firm/{filial_id.strip()}"


def normalize_flamp_url(url: str | None) -> str | None:
    if not url:
        return None

    candidate = url.strip()
    if not candidate:
        return None

    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif candidate.startswith("/"):
        candidate = urljoin(_FLAMP_BASE_URL, candidate)

    parsed = urlparse(candidate)
    if not parsed.scheme:
        candidate = urljoin(_FLAMP_BASE_URL, candidate)
        parsed = urlparse(candidate)

    # Recover from malformed URLs like:
    # https://omsk.flamp.ru//omsk.flamp.ru/firm/company-123
    nested_match = re.search(r"(?:^|/)(?:https?:/)?/?(omsk\.flamp\.ru/firm/.+)$", parsed.path, flags=re.IGNORECASE)
    if parsed.netloc.endswith("flamp.ru") and nested_match:
        nested_path = "/" + nested_match.group(1).split("/", 1)[1]
        return urljoin(_FLAMP_BASE_URL, nested_path)

    if parsed.netloc.endswith("flamp.ru"):
        return candidate

    return candidate


def _deduplicate_reviews(reviews: list[RawReview]) -> list[RawReview]:
    unique: list[RawReview] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for review in reviews:
        key = (
            (review.text or "").strip(),
            (review.author or "").strip(),
            review.review_date.isoformat() if review.review_date else None,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(review)
    return unique


def _parse_jsonld_reviews(soup: BeautifulSoup, page_url: str, source_name: str) -> list[RawReview]:
    reviews: list[RawReview] = []

    for script in soup.select("script[type='application/ld+json']"):
        raw_text = (script.string or script.get_text() or "").strip()
        if not raw_text:
            continue
        try:
            payload = json.loads(raw_text)
        except Exception:
            continue

        for item in _iter_json_nodes(payload):
            body = item.get("reviewBody") or item.get("description")
            if not isinstance(body, str) or not body.strip():
                continue

            review_rating = item.get("reviewRating")
            rating_raw = None
            if isinstance(review_rating, dict):
                rating_raw = review_rating.get("ratingValue")
            elif review_rating is not None:
                rating_raw = review_rating

            author = item.get("author")
            if isinstance(author, dict):
                author = author.get("name")

            reviews.append(
                RawReview(
                    source=source_name,
                    text=body.strip(),
                    rating=_safe_float(rating_raw),
                    author=str(author).strip() if author else None,
                    review_date=_parse_date(item.get("datePublished") or item.get("dateCreated")),
                    source_link=page_url,
                )
            )

    return reviews


def _iter_json_nodes(payload):
    if isinstance(payload, dict):
        if "reviewBody" in payload or "reviewRating" in payload:
            yield payload
        for value in payload.values():
            yield from _iter_json_nodes(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_json_nodes(item)


def _safe_float(raw: str | int | float | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "."))
    except Exception:
        return None
