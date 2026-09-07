"""Review search helpers for Flamp, Otzovik, and VK."""

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
from src.enrichment.review_parsers.flamp import FlampParser, normalize_flamp_url
from src.enrichment.review_parsers.otzovik import OtzovikParser
from src.enrichment.review_parsers.vk import VkParser
from src.proxy import enrichment_proxy_manager, iter_proxy_urls, require_proxy_pool

log = structlog.get_logger(__name__)

_PARSERS: list[AbstractReviewParser] = [
    FlampParser(),
    OtzovikParser(),
    VkParser(),
]

_MAX_URLS_PER_PARSER = 3
_SEARCH_DELAY_S = 2
_CITY_SUFFIX = "\u041e\u043c\u0441\u043a"

_FLAMP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class ReviewSearcher:
    """Searches review URLs and dispatches them to source-specific parsers."""

    async def search(self, raw: CompanyRaw) -> list[RawReview]:
        """Return all external reviews found for a raw company."""
        if not settings.enable_review_enrichment:
            return []

        name = raw.name_raw
        if not name or not name.strip():
            return []

        log.info("review_searcher.start", raw_id=str(raw.id), name=name)

        all_urls: list[str] = []

        if raw.source == "2gis" and raw.source_id:
            flamp_url = _flamp_firm_url(str(raw.source_id))
            all_urls.append(flamp_url)
            log.info("review_searcher.flamp_from_2gis", raw_id=str(raw.id), url=flamp_url)

        if not any("flamp.ru" in url for url in all_urls):
            try:
                all_urls.extend(await self._search_flamp_direct(name, raw.phones))
            except Exception as exc:
                log.warning("review_searcher.flamp_direct.error", error=str(exc))

        queries = self._build_queries(name, raw.phones)
        for index, query in enumerate(queries):
            if index > 0:
                await asyncio.sleep(_SEARCH_DELAY_S)
            all_urls.extend(await self._search_urls(query))

        classified = self._classify_urls(all_urls)
        log.info(
            "review_searcher.urls_found",
            raw_id=str(raw.id),
            by_parser={key: len(value) for key, value in classified.items()},
        )

        reviews = await self._dispatch(classified, name)
        log.info("review_searcher.done", raw_id=str(raw.id), reviews_found=len(reviews))
        return reviews

    def _build_queries(self, name: str, phones: list[str] | None = None) -> list[str]:
        queries = [
            f'"{name}" {_CITY_SUFFIX} \u043e\u0442\u0437\u044b\u0432\u044b',
            f'"{name}" {_CITY_SUFFIX} flamp',
        ]

        if phones:
            for phone in phones[:1]:
                queries.append(f'"{phone}" \u043e\u0442\u0437\u044b\u0432\u044b')
                queries.append(f'"{phone}" flamp')

        return queries[: max(1, settings.review_search_max_queries)]

    async def _search_urls(self, query: str) -> list[str]:
        """Search URLs via DDG and/or SerpAPI, respecting config flags."""
        require_proxy_pool(enrichment_proxy_manager, purpose="review search")

        if not settings.enable_ddg_search:
            log.debug("review_searcher.ddg_disabled")
            if settings.serpapi_key:
                return await self._serpapi_search(query)
            return []

        urls = await self._ddg_search_safe(query)
        if urls is None:
            if settings.serpapi_key:
                log.info("review_searcher.serpapi_fallback", query=query)
                return await self._serpapi_search(query)
            return []
        return urls

    async def _ddg_search_safe(self, query: str) -> list[str] | None:
        try:
            return await self._ddg_search(query)
        except RuntimeError:
            raise
        except Exception:
            return None

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        reraise=True,
    )
    async def _ddg_search(self, query: str) -> list[str]:
        """Run DuckDuckGo search in a thread because the client is synchronous."""
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import DuckDuckGoSearchException, RatelimitException

        def _run() -> list[str]:
            proxy_url = next(iter_proxy_urls(enrichment_proxy_manager, purpose="review_searcher.ddg"))
            with DDGS(proxy=proxy_url) as ddgs:
                results = ddgs.text(query, max_results=10)
                return [result["href"] for result in (results or [])]

        try:
            return await asyncio.to_thread(_run)
        except RatelimitException:
            log.warning("review_searcher.ddg_ratelimit", query=query[:50])
            raise
        except DuckDuckGoSearchException as exc:
            log.warning("review_searcher.ddg_error", query=query[:50], error=str(exc))
            return []

    async def _serpapi_search(self, query: str) -> list[str]:
        params = {
            "q": query,
            "hl": "ru",
            "gl": "ru",
            "api_key": settings.serpapi_key,
            "num": 10,
        }
        last_error = ""
        for proxy_url in iter_proxy_urls(enrichment_proxy_manager, purpose="review_searcher.serpapi"):
            try:
                async with httpx.AsyncClient(timeout=10.0, proxy=proxy_url) as client:
                    response = await client.get("https://serpapi.com/search.json", params=params)
                    response.raise_for_status()
                    data = response.json()
                return [item["link"] for item in data.get("organic_results", [])]
            except Exception as exc:
                last_error = str(exc)
                log.warning("review_searcher.serpapi_error", error=last_error, proxy=proxy_url)
        return []

    def _classify_urls(self, urls: list[str]) -> dict[str, list[str]]:
        """Group URLs by parser, deduplicated and capped per parser."""
        classified: dict[str, list[str]] = {}
        seen: set[str] = set()
        flamp_parser = next((parser for parser in _PARSERS if parser.source_name == "flamp"), None)

        for url in urls:
            normalized_url = url
            if flamp_parser and flamp_parser.can_handle(url):
                normalized_url = normalize_flamp_url(url) or url

            url_key = normalized_url.lower().split("?")[0].rstrip("/")
            if url_key in seen:
                continue
            seen.add(url_key)

            for parser in _PARSERS:
                if parser.can_handle(normalized_url):
                    bucket = classified.setdefault(parser.source_name, [])
                    if len(bucket) < _MAX_URLS_PER_PARSER:
                        bucket.append(normalized_url)
                    break

        return classified

    async def _dispatch(
        self, classified: dict[str, list[str]], company_name: str
    ) -> list[RawReview]:
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

    async def _search_flamp_direct(self, name: str, phones: list[str] | None = None) -> list[str]:
        """Search Flamp directly for firm URLs, bypassing DDG entirely."""
        query_candidates = _build_flamp_queries(name, phones)
        if not query_candidates:
            return []

        proxy_candidates: list[str | None] = []
        max_attempts = max(1, int(settings.flamp_max_attempts))
        if enrichment_proxy_manager.count > 0:
            proxy_candidates.extend(
                iter_proxy_urls(
                    enrichment_proxy_manager,
                    purpose="review_searcher.flamp",
                    max_attempts=max_attempts,
                )
            )
        # Always do at least one direct try so Flamp is attempted even without proxy pool.
        proxy_candidates.append(None)

        for query in query_candidates:
            for proxy_url in proxy_candidates:
                try:
                    async with httpx.AsyncClient(
                        timeout=max(5.0, float(settings.flamp_timeout_seconds)),
                        headers=_FLAMP_HEADERS,
                        proxy=proxy_url,
                        follow_redirects=True,
                    ) as client:
                        firm_urls = await self._flamp_api_search(client, query)
                        if firm_urls:
                            log.info(
                                "review_searcher.flamp_direct.done",
                                query=query,
                                found=len(firm_urls),
                                proxy=proxy_url or "direct",
                            )
                            return firm_urls
                        firm_urls = await self._flamp_html_search(client, query)
                        if firm_urls:
                            log.info(
                                "review_searcher.flamp_direct.done",
                                query=query,
                                found=len(firm_urls),
                                proxy=proxy_url or "direct",
                                fallback="html",
                            )
                            return firm_urls
                except Exception as exc:
                    log.warning("review_searcher.flamp_proxy_error", error=str(exc), proxy=proxy_url or "direct")
        return []

    async def _flamp_api_search(self, client: httpx.AsyncClient, query: str) -> list[str]:
        try:
            response = await client.get(
                "https://omsk.flamp.ru/api/2.0/filials",
                params={"q": query, "limit": 5, "city": "omsk"},
            )
            if response.status_code != 200:
                log.debug("flamp_api_search.status", status=response.status_code)
                return []

            data = response.json()
            urls: list[str] = []
            for item in data.get("results") or data.get("items") or []:
                filial_id = item.get("id")
                if filial_id:
                    urls.append(_flamp_firm_url(str(filial_id)))
            log.info("flamp_api_search.done", query=query, found=len(urls))
            return urls[:3]
        except Exception as exc:
            log.debug("flamp_api_search.error", error=str(exc))
            return []

    async def _flamp_html_search(self, client: httpx.AsyncClient, query: str) -> list[str]:
        search_url = f"https://omsk.flamp.ru/search/{quote(query)}"
        try:
            response = await client.get(search_url)
            if response.status_code != 200:
                log.debug("flamp_html_search.status", status=response.status_code, url=search_url)
                return []

            soup = BeautifulSoup(response.text, "lxml")
            firm_urls: list[str] = []

            for anchor in soup.select("a[href*='/firm/']"):
                href = anchor.get("href", "")
                if "/firm/" not in href:
                    continue
                normalized_href = normalize_flamp_url(href)
                if normalized_href and "flamp.ru/firm/" in normalized_href and normalized_href not in firm_urls:
                    firm_urls.append(normalized_href)

            if not firm_urls:
                for script in soup.select("script"):
                    text = script.string or ""
                    for match in re.finditer(r'"id"\s*:\s*"?(\d{8,})"?', text):
                        url = _flamp_firm_url(match.group(1))
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
    """Strip common prefixes and suffixes from company names for search."""
    name = name.strip().strip("\"'\u00ab\u00bb")
    name = re.sub(
        r"^(?:\u041e\u041e\u041e|\u041e\u0410\u041e|\u0417\u0410\u041e|\u0418\u041f|\u0410\u041e|\u041f\u0410\u041e)\s+",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"\s*(?:\u2014|\u2013|-)\s+.+$", "", name)
    name = re.sub(rf"\s+{_CITY_SUFFIX}\s*$", "", name, flags=re.IGNORECASE)
    name = re.split(r"\s*[|/]\s*", name, maxsplit=1)[0]
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _build_flamp_queries(name: str, phones: list[str] | None = None) -> list[str]:
    clean_name = _clean_company_name(name)
    if not clean_name or len(clean_name) < 3:
        return []

    queries: list[str] = [clean_name]

    split_parts = [
        part.strip()
        for part in re.split(r"[,;/|]+", name)
        if len(part.strip()) >= 3
    ]
    for part in split_parts:
        normalized_part = _clean_company_name(part)
        if normalized_part and normalized_part not in queries:
            queries.append(normalized_part)

    for phone in phones or []:
        digits = re.sub(r"\D", "", phone)
        if len(digits) < 10:
            continue
        e164 = f"+{digits}" if not phone.strip().startswith("+") else phone.strip()
        if e164 not in queries:
            queries.append(e164)
        break

    return queries[:3]


def _flamp_firm_url(filial_id: str) -> str:
    return f"https://omsk.flamp.ru/firm/{filial_id.strip()}"
