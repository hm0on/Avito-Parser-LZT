"""ReviewSearcher — finds review URLs via DuckDuckGo/SerpAPI and dispatches to parsers."""

from __future__ import annotations

import asyncio

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.collectors.base import RawReview
from src.config import settings
from src.database.models import CompanyRaw
from src.enrichment.review_parsers.base import AbstractReviewParser
from src.enrichment.review_parsers.flamp import FlampParser
from src.enrichment.review_parsers.otzovik import OtzovikParser
from src.enrichment.review_parsers.vk import VkParser

log = structlog.get_logger(__name__)

_PARSERS: list[AbstractReviewParser] = [
    FlampParser(),
    OtzovikParser(),
    VkParser(),
]

_MAX_URLS_PER_PARSER = 2
_SEARCH_DELAY_S = 2  # seconds between DDG queries to avoid rate limiting


class ReviewSearcher:
    """Searches for additional review pages and dispatches to platform parsers."""

    async def search(self, raw: CompanyRaw) -> list[RawReview]:
        """Entry point: search for reviews and return all found RawReview objects."""
        if not settings.enable_review_enrichment:
            return []

        name = raw.name_raw
        if not name or not name.strip():
            return []

        log.info("review_searcher.start", raw_id=str(raw.id), name=name)

        queries = self._build_queries(name, raw.phones)
        all_urls: set[str] = set()

        for i, query in enumerate(queries):
            if i > 0:
                await asyncio.sleep(_SEARCH_DELAY_S)
            urls = await self._search_urls(query)
            all_urls.update(urls)

        classified = self._classify_urls(list(all_urls))
        log.info(
            "review_searcher.urls_found",
            raw_id=str(raw.id),
            by_parser={k: len(v) for k, v in classified.items()},
        )

        reviews = await self._dispatch(classified, name)
        log.info("review_searcher.done", raw_id=str(raw.id), reviews_found=len(reviews))
        return reviews

    def _build_queries(self, name: str, phones: list[str] | None = None) -> list[str]:
        queries = [
            f'"{name}" Омск отзывы',
            f'"{name}" Омск flamp',
        ]

        # Add phone-based queries for better results with generic names
        if phones:
            for phone in phones[:1]:  # Only use first phone to avoid too many queries
                queries.append(f'"{phone}" отзывы')
                queries.append(f'"{phone}" flamp')

        return queries

    async def _search_urls(self, query: str) -> list[str]:
        """Try DDG, fall back to SerpAPI if DDG fails and key is available."""
        urls = await self._ddg_search_safe(query)
        if urls is None:
            # DDG exhausted all retries
            if settings.serpapi_key:
                log.info("review_searcher.serpapi_fallback", query=query)
                return await self._serpapi_search(query)
            return []
        return urls

    async def _ddg_search_safe(self, query: str) -> list[str] | None:
        """Returns list of URLs, or None if all retries were exhausted."""
        try:
            return await self._ddg_search(query)
        except Exception:
            return None

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        reraise=True,
    )
    async def _ddg_search(self, query: str) -> list[str]:
        """Run DuckDuckGo search in a thread (DDG library is synchronous)."""
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import DuckDuckGoSearchException, RatelimitException

        def _run() -> list[str]:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=10)
                return [r["href"] for r in (results or [])]

        try:
            return await asyncio.to_thread(_run)
        except RatelimitException as exc:
            log.warning("review_searcher.ddg_ratelimit", query=query[:50])
            raise  # tenacity will retry
        except DuckDuckGoSearchException as exc:
            log.warning("review_searcher.ddg_error", query=query[:50], error=str(exc))
            return []

    async def _serpapi_search(self, query: str) -> list[str]:
        """Use SerpAPI as a fallback search provider."""
        params = {
            "q": query,
            "hl": "ru",
            "gl": "ru",
            "api_key": settings.serpapi_key,
            "num": 10,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get("https://serpapi.com/search.json", params=params)
                resp.raise_for_status()
                data = resp.json()
            return [r["link"] for r in data.get("organic_results", [])]
        except Exception as exc:
            log.warning("review_searcher.serpapi_error", error=str(exc))
            return []

    def _classify_urls(self, urls: list[str]) -> dict[str, list[str]]:
        """Group URLs by parser, deduplicated, capped at _MAX_URLS_PER_PARSER each."""
        classified: dict[str, list[str]] = {}
        seen: set[str] = set()

        for url in urls:
            # Normalize for dedup: lowercase netloc+path
            url_key = url.lower().split("?")[0].rstrip("/")
            if url_key in seen:
                continue
            seen.add(url_key)

            for parser in _PARSERS:
                if parser.can_handle(url):
                    bucket = classified.setdefault(parser.source_name, [])
                    if len(bucket) < _MAX_URLS_PER_PARSER:
                        bucket.append(url)
                    break

        return classified

    async def _dispatch(
        self, classified: dict[str, list[str]], company_name: str
    ) -> list[RawReview]:
        """Call safe_parse() for all classified URLs concurrently."""
        tasks = []
        for parser in _PARSERS:
            for url in classified.get(parser.source_name, []):
                tasks.append(parser.safe_parse(url, company_name))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks)
        combined: list[RawReview] = []
        for batch in results:
            combined.extend(batch)
        return combined
