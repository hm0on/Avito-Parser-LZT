"""ReviewSearcher — finds review URLs via direct Flamp search + DuckDuckGo/SerpAPI."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import httpx
import structlog
from bs4 import BeautifulSoup
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

_MAX_URLS_PER_PARSER = 3
_SEARCH_DELAY_S = 2  # seconds between DDG queries to avoid rate limiting

_FLAMP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class ReviewSearcher:
    """Searches for additional review pages and dispatches to platform parsers."""

    async def search(self, raw: CompanyRaw) -> list[RawReview]:
        """Entry point: search for reviews and return all found RawReview objects.

        Search strategy (ordered by reliability):
        1. Direct Flamp URL from 2GIS branch_id (filial_id == branch_id)
        2. Direct Flamp search by company name (bypasses DDG)
        3. DDG search for all platforms (Flamp, Otzovik, VK)
        """
        if not settings.enable_review_enrichment:
            return []

        name = raw.name_raw
        if not name or not name.strip():
            return []

        log.info("review_searcher.start", raw_id=str(raw.id), name=name)

        # Ordered list — direct Flamp URLs first so they get priority in _classify_urls
        all_urls: list[str] = []

        # 1. For 2GIS companies: construct Flamp URL from branch_id (same ID system)
        if raw.source == "2gis" and raw.source_id:
            flamp_url = f"https://omsk.flamp.ru/firm/-{raw.source_id}"
            all_urls.append(flamp_url)
            log.info("review_searcher.flamp_from_2gis", raw_id=str(raw.id), url=flamp_url)

        # 2. Cross-search via 2GIS API (for non-2GIS companies, e.g. Avito)
        #    Finds the company on 2GIS → branch_id → Flamp firm URL
        if raw.source != "2gis":
            try:
                twogis_urls = await self._search_via_2gis_api(name)
                all_urls.extend(twogis_urls)
            except Exception as exc:
                log.warning("review_searcher.2gis_cross.error", error=str(exc))

        # 3. Direct Flamp search by company name
        if not any("flamp.ru" in u for u in all_urls):
            try:
                flamp_urls = await self._search_flamp_direct(name)
                all_urls.extend(flamp_urls)
            except Exception as exc:
                log.warning("review_searcher.flamp_direct.error", error=str(exc))

        # 4. DDG search for all platforms (Otzovik, VK, etc.)
        queries = self._build_queries(name, raw.phones)
        for i, query in enumerate(queries):
            if i > 0:
                await asyncio.sleep(_SEARCH_DELAY_S)
            urls = await self._search_urls(query)
            all_urls.extend(urls)

        classified = self._classify_urls(all_urls)
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

    # ── 2GIS cross-search → Flamp URL ──────────────────────────────────

    async def _search_via_2gis_api(self, name: str) -> list[str]:
        """Search 2GIS catalog API for company, return Flamp firm URLs.

        Uses the cached 2GIS API key from TwoGisCollector (captured during Stage 1).
        """
        from src.collectors.twogis import CATALOG_URL, TwoGisCollector

        api_key = TwoGisCollector._api_key
        if not api_key:
            log.debug("review_searcher.2gis_cross.no_key")
            return []

        clean_name = _clean_company_name(name)
        if not clean_name or len(clean_name) < 3:
            return []

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    CATALOG_URL,
                    params={
                        "q": clean_name,
                        "region_id": settings.twogis_region_id,
                        "key": api_key,
                        "page_size": 3,
                        "type": "branch",
                    },
                )
                if resp.status_code in (401, 403):
                    TwoGisCollector._api_key = None
                    return []
                if resp.status_code != 200:
                    return []

                data = resp.json()
                items = data.get("result", {}).get("items", [])
                urls = [
                    f"https://omsk.flamp.ru/firm/-{item['id']}"
                    for item in items
                    if item.get("id")
                ]
                log.info("review_searcher.2gis_cross.done", query=clean_name, found=len(urls))
                return urls[:3]
        except Exception as exc:
            log.debug("review_searcher.2gis_cross.error", error=str(exc))
            return []

    # ── Direct Flamp search (bypass DDG) ─────────────────────────────────

    async def _search_flamp_direct(self, name: str) -> list[str]:
        """Search Flamp directly for firm URLs, bypassing DDG entirely."""
        clean_name = _clean_company_name(name)
        if not clean_name or len(clean_name) < 3:
            return []

        from src.proxy import proxy_manager

        proxy_url = proxy_manager.get_next()

        async with httpx.AsyncClient(
            timeout=15.0,
            headers=_FLAMP_HEADERS,
            proxy=proxy_url or None,
            follow_redirects=True,
        ) as client:
            # Try Flamp internal API first (fast, structured)
            firm_urls = await self._flamp_api_search(client, clean_name)
            if firm_urls:
                return firm_urls

            # Fallback: scrape HTML search page
            return await self._flamp_html_search(client, clean_name)

    async def _flamp_api_search(
        self, client: httpx.AsyncClient, query: str
    ) -> list[str]:
        """Try Flamp's internal filial search API."""
        try:
            resp = await client.get(
                "https://omsk.flamp.ru/api/2.0/filials",
                params={"q": query, "limit": 5, "city": "omsk"},
            )
            if resp.status_code != 200:
                log.debug("flamp_api_search.status", status=resp.status_code)
                return []

            data = resp.json()
            urls: list[str] = []
            for item in data.get("results") or data.get("items") or []:
                filial_id = item.get("id")
                if filial_id:
                    urls.append(f"https://omsk.flamp.ru/firm/-{filial_id}")
            log.info("flamp_api_search.done", query=query, found=len(urls))
            return urls[:3]
        except Exception as exc:
            log.debug("flamp_api_search.error", error=str(exc))
            return []

    async def _flamp_html_search(
        self, client: httpx.AsyncClient, query: str
    ) -> list[str]:
        """Scrape Flamp search results page for firm URLs."""
        search_url = f"https://omsk.flamp.ru/search/{quote(query)}"
        try:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                log.debug("flamp_html_search.status", status=resp.status_code, url=search_url)
                return []

            soup = BeautifulSoup(resp.text, "lxml")
            firm_urls: list[str] = []

            # Method 1: find <a> tags with /firm/ in href
            for a in soup.select("a[href*='/firm/']"):
                href = a.get("href", "")
                if "/firm/" in href:
                    if href.startswith("/"):
                        href = f"https://omsk.flamp.ru{href}"
                    if "flamp.ru/firm/" in href and href not in firm_urls:
                        firm_urls.append(href)

            # Method 2: extract filial IDs from embedded JSON in <script> tags
            if not firm_urls:
                for script in soup.select("script"):
                    text = script.string or ""
                    for m in re.finditer(r'"id"\s*:\s*"?(\d{8,})"?', text):
                        fid = m.group(1)
                        url = f"https://omsk.flamp.ru/firm/-{fid}"
                        if url not in firm_urls:
                            firm_urls.append(url)
                    if firm_urls:
                        break

            log.info("flamp_html_search.done", query=query, found=len(firm_urls))
            return firm_urls[:3]
        except Exception as exc:
            log.debug("flamp_html_search.error", error=str(exc))
            return []


def _clean_company_name(name: str) -> str:
    """Strip common prefixes/suffixes from company names for better search.

    Examples:
        'ООО БурПро — Бурение скважин Омск' → 'БурПро'
        'ИП Петров Сантехника' → 'Петров Сантехника'
    """
    name = name.strip().strip("\"'«»")
    # Remove organizational prefixes
    name = re.sub(r"^(ООО|ОАО|ЗАО|ИП|АО|ПАО)\s+", "", name, flags=re.IGNORECASE)
    # Remove " — description" suffix (common in Avito names)
    name = re.sub(r"\s*[—–-]\s+.+$", "", name)
    # Remove city suffix
    name = re.sub(r"\s+Омск\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()
