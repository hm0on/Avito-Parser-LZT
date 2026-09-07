"""2GIS UI-only scraper using Playwright and the dedicated proxy file."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import require_proxy_pool, twogis_proxy_manager

log = structlog.get_logger(__name__)

# ── Proxy auth error detection ───────────────────────────────────────────

_PROXY_AUTH_MARKERS = [
    "err_proxy_auth_unsupported",
    "err_proxy_authentication_required",
    "proxy authentication required",
    "407",
    "err_tunnel_connection_failed",
    "proxy auth",
]


class TwoGisProxyAuthError(RuntimeError):
    """All 2GIS proxies failed authentication — collection cannot proceed."""


def _is_proxy_auth_error(error_text: str) -> bool:
    """Classify whether a Playwright/network error indicates proxy auth failure."""
    lower = (error_text or "").lower()
    return any(marker in lower for marker in _PROXY_AUTH_MARKERS)


# ── Precheck constants ───────────────────────────────────────────────────

_PRECHECK_URL = "https://2gis.ru/omsk"
_PRECHECK_SAMPLE_SIZE = 3
_PRECHECK_TIMEOUT = 12.0


def _rewrite_proxy_scheme(proxy_url: str, target_scheme: str) -> str:
    """Rewrite the scheme of a proxy URL (e.g. http:// → socks5://).

    For socks5, credentials must be embedded in the URL because httpx sends
    socks auth at the SOCKS handshake level, not via HTTP Proxy-Authorization.
    """
    if not proxy_url or not target_scheme:
        return proxy_url
    from urllib.parse import urlparse, unquote, quote

    parsed = urlparse(proxy_url)
    current_scheme = (parsed.scheme or "http").lower()
    target = target_scheme.strip().lower()
    if current_scheme == target:
        return proxy_url

    # Rebuild URL with new scheme, always embedding credentials
    auth = ""
    if parsed.username:
        user = quote(unquote(parsed.username), safe="")
        pwd = quote(unquote(parsed.password or ""), safe="")
        auth = f"{user}:{pwd}@"
    return f"{target}://{auth}{parsed.hostname}:{parsed.port}"


# Lazy import to avoid circular deps
_flamp_parser = None


def _get_flamp_parser():
    global _flamp_parser
    if _flamp_parser is None:
        from src.enrichment.review_parsers.flamp import FlampParser
        _flamp_parser = FlampParser()
    return _flamp_parser

_SEARCH_URL = "https://2gis.ru/omsk/search/{query}"


class TwoGisCollector(AbstractCollector):
    source_name = "2gis"

    async def collect(self, keyword: str, *, max_companies: int = 0) -> list[RawCompany]:
        log.info("twogis.collect.start", keyword=keyword)
        require_proxy_pool(twogis_proxy_manager, purpose="2GIS collection")

        queries = self._build_geo_queries(keyword)
        all_companies: list[RawCompany] = []
        seen_ids: set[str] = set()
        seen_links: set[str] = set()

        for query in queries:
            remaining = max(0, max_companies - len(all_companies)) if max_companies else 0
            if max_companies and remaining == 0:
                break
            companies = await self._collect_via_ui(query, max_companies=remaining)
            for company in companies:
                source_id = (company.source_id or "").strip()
                source_link = (company.source_link or "").strip()
                if source_id and source_id in seen_ids:
                    continue
                if source_link and source_link in seen_links:
                    continue
                if source_id:
                    seen_ids.add(source_id)
                if source_link:
                    seen_links.add(source_link)
                all_companies.append(company)
                if max_companies and len(all_companies) >= max_companies:
                    break
            if max_companies and len(all_companies) >= max_companies:
                break

        log.info("twogis.collect.done", keyword=keyword, count=len(all_companies), mode="ui", queries=len(queries))
        return all_companies

    def _build_geo_queries(self, keyword: str) -> list[str]:
        variants: list[str] = []
        for area in settings.twogis_search_areas_list:
            area_norm = area.strip().replace("_", " ").replace("-", " ")
            if not area_norm:
                continue
            if area_norm.lower() in keyword.lower():
                variants.append(keyword)
            else:
                variants.append(f"{keyword} {area_norm}")
        variants.append(keyword)
        return list(dict.fromkeys(variants))

    async def precheck_proxies(self):
        """Lightweight proxy validation before keyword loop.

        Tests a sample of proxies with a HEAD/GET to 2gis.ru.
        Raises ProxyPoolExhaustedError (via TwoGisProxyAuthError) if none pass.
        Returns a simple summary object compatible with source_runner expectations.
        """
        from src.collectors.avito_proxy_precheck import ProxyPoolExhaustedError

        all_proxies = list(twogis_proxy_manager._proxies)
        sample = all_proxies[:_PRECHECK_SAMPLE_SIZE] if len(all_proxies) > _PRECHECK_SAMPLE_SIZE else all_proxies

        log.info("twogis.proxy.precheck.start", total_pool=len(all_proxies), sample_size=len(sample))

        ok_count = 0
        auth_fail_count = 0
        timeout_count = 0
        other_fail_count = 0

        scheme = settings.twogis_proxy_scheme
        for raw_proxy_url in sample:
            proxy_url = _rewrite_proxy_scheme(raw_proxy_url, scheme) if scheme else raw_proxy_url
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url,
                    timeout=httpx.Timeout(_PRECHECK_TIMEOUT),
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(
                        _PRECHECK_URL,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"},
                    )
                if resp.status_code == 407:
                    auth_fail_count += 1
                    log.warning("twogis.proxy.auth_failed", proxy=proxy_url, status=407)
                elif resp.status_code in (401, 403):
                    auth_fail_count += 1
                    log.warning("twogis.proxy.auth_failed", proxy=proxy_url, status=resp.status_code)
                else:
                    ok_count += 1
            except httpx.ProxyError as exc:
                if _is_proxy_auth_error(str(exc)):
                    auth_fail_count += 1
                    log.warning("twogis.proxy.auth_failed", proxy=proxy_url, error=str(exc)[:200])
                else:
                    other_fail_count += 1
            except (httpx.TimeoutException, httpx.ConnectError):
                timeout_count += 1
            except Exception:
                other_fail_count += 1

        log.info(
            "twogis.proxy.precheck.done",
            ok=ok_count,
            auth_fail=auth_fail_count,
            timeout=timeout_count,
            other_fail=other_fail_count,
        )

        if ok_count == 0:
            detail = f"0/{len(sample)} proxies passed"
            if auth_fail_count > 0:
                detail += f" ({auth_fail_count} auth/407 failures)"
            raise ProxyPoolExhaustedError(
                f"2GIS proxy pool unauthorized: {detail}. "
                "Check storage/proxies/twogis.txt credentials."
            )

        # Return a duck-typed summary compatible with source_runner log
        class _Summary:
            total_tested = len(sample)
            avito_ok = ok_count  # reuse the field name source_runner expects
            ranked_proxies = all_proxies[:ok_count]

        return _Summary()

    async def _collect_via_ui(self, keyword: str, *, max_companies: int = 0) -> list[RawCompany]:
        query = quote_plus(keyword)
        search_url = _SEARCH_URL.format(query=query)
        use_proxy_attempts = min(
            twogis_proxy_manager.count,
            max(1, settings.twogis_max_proxy_rotations),
        )

        auth_failures = 0
        for attempt in range(1, use_proxy_attempts + 1):
            companies, failure_reason = await self._collect_via_ui_once_classified(
                keyword,
                search_url,
                max_companies=max_companies,
            )
            if companies:
                return companies

            if failure_reason == "proxy_auth":
                auth_failures += 1
                log.warning("twogis.proxy.auth_failed", attempt=attempt, max_attempts=use_proxy_attempts)
                # If consecutive auth failures exceed threshold, fail fast
                if auth_failures >= min(3, use_proxy_attempts):
                    log.error(
                        "twogis.collect.fail_fast",
                        keyword=keyword,
                        auth_failures=auth_failures,
                        reason="all proxies failed proxy authentication",
                    )
                    raise TwoGisProxyAuthError(
                        f"2GIS proxy pool unauthorized: {auth_failures}/{attempt} attempts "
                        "failed with proxy auth errors (407/ERR_PROXY_AUTH). "
                        "Check storage/proxies/twogis.txt credentials."
                    )
            else:
                auth_failures = 0  # reset on non-auth failure

            if attempt < use_proxy_attempts:
                log.info("twogis.ui.proxy_retry", attempt=attempt, next_attempt=attempt + 1, max_attempts=use_proxy_attempts)
        return []

    async def _collect_via_ui_once_classified(
        self,
        keyword: str,
        search_url: str,
        *,
        max_companies: int = 0,
    ) -> tuple[list[RawCompany], str | None]:
        """Wrapper around _collect_via_ui_once that classifies failure reason.

        Returns (companies, failure_reason) where failure_reason is one of:
        - None if companies were found
        - "proxy_auth" for proxy authentication errors
        - "blocked" for captcha/forbidden/http blocks
        - "empty" for no results
        - "error" for unexpected errors
        """
        try:
            companies, failure = await self._collect_via_ui_once(
                keyword,
                search_url,
                max_companies=max_companies,
            )
            if companies:
                return companies, None
            return [], failure or "empty"
        except Exception as exc:
            if _is_proxy_auth_error(str(exc)):
                return [], "proxy_auth"
            return [], "error"

    async def _collect_via_ui_once(
        self,
        keyword: str,
        search_url: str,
        *,
        max_companies: int = 0,
    ) -> tuple[list[RawCompany], str | None]:
        """Returns (companies, failure_reason). failure_reason is None on success."""
        async with async_playwright() as pw:
            proxy = twogis_proxy_manager.playwright_proxy(
                scheme=settings.twogis_proxy_scheme or None,
            )
            headless = not settings.twogis_debug_browser
            browser = await pw.chromium.launch(
                headless=headless,
                proxy=proxy,
                slow_mo=300 if settings.twogis_debug_browser else 0,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="ru-RU",
                )
                page = await context.new_page()
                try:
                    resp = await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
                    await asyncio.sleep(2)
                except Exception as exc:
                    error_str = str(exc)
                    if _is_proxy_auth_error(error_str):
                        log.warning("twogis.ui.goto_proxy_auth_error", error=error_str[:200])
                        return [], "proxy_auth"
                    log.warning("twogis.ui.goto_error", error=error_str)
                    return [], "error"
                if resp and resp.status == 407:
                    log.warning("twogis.ui.proxy_407", status=resp.status)
                    return [], "proxy_auth"
                if resp and resp.status in (401, 403, 429, 503):
                    log.warning("twogis.ui.blocked_status", status=resp.status)
                    return [], "blocked"

                if "captcha.2gis.ru" in page.url:
                    log.warning("twogis.ui.captcha")
                    return [], "blocked"
                first_html = await page.content()
                if _is_forbidden_html(first_html):
                    log.warning("twogis.ui.forbidden_page")
                    return [], "blocked"

                search_pages = await self._collect_search_pages(page, search_url)
                log.info("twogis.ui.pages_collected", count=len(search_pages))

                firm_urls = await self._collect_firm_urls_from_pages(
                    page,
                    search_pages,
                    max_firms=max_companies,
                )
                if not firm_urls:
                    log.info("twogis.ui.no_firms")
                    return [], "empty"

                companies = await self._scrape_firm_pages_parallel(
                    pw,
                    firm_urls[: min(120, max_companies)] if max_companies else firm_urls[:120],
                    keyword,
                )
                return companies, None if companies else "empty"
            finally:
                await browser.close()

    async def _collect_search_pages(self, page, search_url: str) -> list[str]:
        base = _normalize_search_base(page.url.split("?")[0], fallback=search_url.split("?")[0])
        max_page = await _detect_max_search_page(page)
        if max_page <= 0:
            max_page = max(1, settings.twogis_search_max_pages)
        else:
            max_page = min(max_page, max(1, settings.twogis_search_max_pages))
        return [base] + [f"{base}/page/{idx}" for idx in range(2, max_page + 1)]

    async def _collect_firm_urls_from_pages(
        self,
        page,
        page_urls: Iterable[str],
        *,
        max_firms: int = 0,
    ) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        for idx, page_url in enumerate(page_urls, start=1):
            if max_firms and len(urls) >= max_firms:
                break
            expected_page = _extract_page_number(page_url)
            try:
                resp = await page.goto(page_url, wait_until="domcontentloaded", timeout=40_000)
                await asyncio.sleep(0.9)
            except Exception as exc:
                log.debug("twogis.ui.page_skip", page=page_url, index=idx, error=str(exc))
                continue
            if resp and resp.status in (401, 403, 429, 503):
                log.warning("twogis.ui.page_blocked_status", page=page_url, index=idx, status=resp.status)
                return []
            if "captcha.2gis.ru" in page.url:
                log.warning("twogis.ui.captcha_on_page", page=page_url, index=idx)
                break
            html = await page.content()
            if _is_forbidden_html(html):
                log.warning("twogis.ui.page_forbidden", page=page_url, index=idx)
                return []
            actual_page = _extract_page_number(page.url.split("?")[0])
            if expected_page and expected_page > 1 and actual_page != expected_page:
                log.info(
                    "twogis.ui.pagination_end",
                    requested=expected_page,
                    actual=actual_page,
                    page=page.url,
                )
                break

            stable_rounds = 0
            for _ in range(25):
                found = await page.evaluate(
                    """() => {
                        const out = [];
                        for (const a of document.querySelectorAll('a[href*="/firm/"]')) {
                            if (!a.href) continue;
                            const clean = a.href.split('?')[0];
                            if (/\\/firm\\/\\d+/.test(clean)) out.push(clean);
                        }
                        return out;
                    }"""
                )
                added = 0
                for url in found:
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
                        added += 1
                        if max_firms and len(urls) >= max_firms:
                            break
                if max_firms and len(urls) >= max_firms:
                    break

                await page.evaluate(
                    """() => {
                        window.scrollBy(0, 1400);
                        for (const el of document.querySelectorAll('div')) {
                            if (el.scrollHeight > el.clientHeight && el.clientHeight > 200) {
                                el.scrollTop += 1000;
                            }
                        }
                    }"""
                )
                await asyncio.sleep(0.7)

                if added == 0:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                if stable_rounds >= 4:
                    break
            if max_firms and len(urls) >= max_firms:
                break

        return urls

    async def _scrape_firm_pages_parallel(
        self,
        pw,
        firm_urls: list[str],
        keyword: str,
    ) -> list[RawCompany]:
        if not firm_urls:
            return []

        workers = max(1, settings.twogis_firm_workers)
        workers = min(workers, len(firm_urls), twogis_proxy_manager.count)

        batches = [firm_urls[i::workers] for i in range(workers)]
        max_retry_attempts = max(1, settings.twogis_company_retry_attempts)
        tabs_per_browser = max(1, settings.twogis_tabs_per_browser)
        tasks = [
            self._scrape_firm_batch(
                pw,
                batch,
                keyword,
                worker_idx=idx + 1,
                tabs_per_browser=tabs_per_browser,
                max_retry_attempts=max_retry_attempts,
            )
            for idx, batch in enumerate(batches)
            if batch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        out: list[RawCompany] = []
        for result in results:
            if isinstance(result, Exception):
                log.warning("twogis.ui.batch_error", error=str(result))
                continue
            out.extend(result)

        # Log review extraction ratio
        with_declared_reviews = sum(1 for c in out if (c.reviews_count or 0) > 0)
        with_actual_reviews = sum(1 for c in out if c.reviews)
        if with_declared_reviews > 0:
            ratio = with_actual_reviews / with_declared_reviews
            log.info(
                "twogis.reviews.extraction_ratio",
                with_declared=with_declared_reviews,
                with_actual=with_actual_reviews,
                ratio=round(ratio, 2),
            )

        return out

    async def _scrape_firm_batch(
        self,
        pw,
        firm_urls: list[str],
        keyword: str,
        *,
        worker_idx: int,
        tabs_per_browser: int,
        max_retry_attempts: int,
    ) -> list[RawCompany]:
        headless = not settings.twogis_debug_browser
        pending_urls = list(firm_urls)
        out: list[RawCompany] = []
        total_attempts = max(
            max_retry_attempts,
            max_retry_attempts + max(1, settings.twogis_max_proxy_rotations) if settings.twogis_retry_until_success else max_retry_attempts,
        )

        last_proxy_server: str | None = None
        for attempt in range(1, total_attempts + 1):
            if not pending_urls:
                break

            proxy = twogis_proxy_manager.playwright_proxy(
                scheme=settings.twogis_proxy_scheme or None,
            )
            last_proxy_server = proxy.get("server") if proxy else None
            browser = await pw.chromium.launch(
                headless=headless,
                proxy=proxy,
                slow_mo=300 if settings.twogis_debug_browser else 0,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="ru-RU",
                )
                sem = asyncio.Semaphore(tabs_per_browser)
                next_pending: list[str] = []

                async def _job(url: str) -> None:
                    async with sem:
                        company, retry_needed = await self._scrape_firm_page_status(context, url, keyword)
                    if company:
                        out.append(company)
                    elif retry_needed:
                        next_pending.append(url)

                await asyncio.gather(*[_job(url) for url in pending_urls])
                pending_urls = next_pending
            finally:
                await browser.close()

            if pending_urls and attempt < total_attempts:
                log.info(
                    "twogis.ui.batch_retry",
                    worker=worker_idx,
                    current_attempt=attempt,
                    next_attempt=attempt + 1,
                    max_attempts=total_attempts,
                    pending=len(pending_urls),
                    proxy=last_proxy_server,
                )
                await asyncio.sleep(0.5)

        log.info(
            "twogis.ui.batch_done",
            worker=worker_idx,
            proxy=last_proxy_server,
            parsed=len(out),
            total=len(firm_urls),
            unresolved=len(pending_urls),
        )
        return out

    async def _scrape_firm_page_status(
        self,
        context,
        url: str,
        keyword: str,
    ) -> tuple[RawCompany | None, bool]:
        page = await context.new_page()
        html = ""
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            await asyncio.sleep(0.5)
            if resp and resp.status in (401, 403, 429, 503):
                return None, True
            if "captcha.2gis.ru" in page.url:
                return None, True
            html = await page.content()
            if _is_forbidden_html(html):
                return None, True
        except Exception:
            return None, True
        finally:
            await page.close()

        soup = BeautifulSoup(html, "html.parser")
        name = _text(soup.select_one("h1")) or _extract_title_fallback(soup)
        if not name:
            return None, False

        source_id = _extract_firm_id(url)
        phones = _extract_phones(soup)
        addresses = _extract_addresses(soup)
        average_rating, reviews_count = _extract_rating_info(soup)
        websites = _extract_websites(soup)
        inn, ogrn = _extract_tax_ids(soup, html)

        # Priority 1: Native 2GIS reviews API
        reviews: list[RawReview] = []
        review_source_marker = "2gis_none"
        if source_id:
            reviews = await self._fetch_native_api_reviews(
                source_id, html, source_link=url,
            )
        if reviews:
            review_source_marker = "2gis_native_api"

        # Priority 2: DOM extraction fallback
        if not reviews:
            reviews = _extract_reviews_from_initial_state(
                html,
                source_id=source_id,
                source_link=url,
            )
            if reviews:
                review_source_marker = "2gis_initial_state"
                log.info(
                    "twogis.reviews.initial_state.done",
                    source_id=source_id,
                    count=len(reviews),
                )

        # Priority 3: DOM extraction fallback
        if not reviews:
            reviews = _extract_reviews_from_soup(soup, source_link=url)
            if reviews:
                review_source_marker = "2gis_dom"
                log.info(
                    "twogis.reviews.dom_fallback.done",
                    source_id=source_id,
                    count=len(reviews),
                )

        # Priority 4: Flamp fallback
        if not reviews and (reviews_count or 0) > 0 and source_id and settings.twogis_flamp_fallback_enabled:
            flamp_reviews = await self._fetch_flamp_fallback(source_id, name or "")
            if flamp_reviews:
                reviews = flamp_reviews
                review_source_marker = "2gis_flamp_fallback"
                log.info(
                    "twogis.flamp_fallback.done",
                    source_id=source_id,
                    name=name,
                    reviews=len(reviews),
                )

        # Recompute count/rating from actual reviews
        if reviews:
            reviews_count = len(reviews)
            ratings = [r.rating for r in reviews if r.rating is not None]
            if ratings:
                average_rating = round(sum(ratings) / len(ratings), 2)

        company = RawCompany(
            source=self.source_name,
            source_id=source_id,
            source_link=url,
            name_raw=name,
            phones=phones,
            addresses=addresses,
            inn=inn,
            ogrn=ogrn,
            average_rating=average_rating,
            reviews_count=reviews_count,
            reviews=reviews,
            contacts_json={"websites": websites},
            raw_payload={
                "keyword": keyword,
                "mode": "ui",
                "review_source": review_source_marker,
                "tax_ids_extracted": bool(inn or ogrn),
            },
        )
        return company, False

    async def _scrape_firm_page(self, context, url: str, keyword: str) -> RawCompany | None:
        company, _ = await self._scrape_firm_page_status(context, url, keyword)
        return company

    async def _fetch_native_api_reviews(
        self,
        source_id: str,
        html: str,
        *,
        source_link: str,
        max_reviews: int = 50,
    ) -> list[RawReview]:
        """Fetch reviews via 2GIS public reviews API using reviewApiKey from page HTML."""
        api_key = _extract_review_api_key(html)
        if not api_key:
            log.debug("twogis.reviews.native_api.no_key", source_id=source_id)
            return []

        url = f"https://public-api.reviews.2gis.com/2.0/branches/{source_id}/reviews"
        params = {
            "key": api_key,
            "limit": str(min(max_reviews, 50)),
            "is_advertiser": "false",
            "sort_by": "date_created",
        }

        reviews: list[RawReview] = []
        seen_keys: set[str] = set()
        max_retries = min(3, twogis_proxy_manager.count or 1)

        for attempt in range(max_retries):
            raw_proxy_url = twogis_proxy_manager.get_next()
            proxy_url = _rewrite_proxy_scheme(raw_proxy_url, settings.twogis_proxy_scheme) if raw_proxy_url else raw_proxy_url
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url,
                    timeout=httpx.Timeout(15.0),
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url, params=params)

                if resp.status_code in (401, 403):
                    log.warning(
                        "twogis.reviews.native_api.blocked",
                        source_id=source_id,
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    return []

                if resp.status_code == 429:
                    log.warning(
                        "twogis.reviews.native_api.blocked",
                        source_id=source_id,
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    continue

                if resp.status_code != 200:
                    log.warning(
                        "twogis.reviews.native_api.http_error",
                        source_id=source_id,
                        status=resp.status_code,
                    )
                    return []

                data = resp.json()
                for item in data.get("reviews", []):
                    text = (item.get("text") or "").strip()
                    if not text or len(text) < 10:
                        continue
                    dedup_key = text.lower()[:160]
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    rating_val = item.get("rating")
                    rating = float(rating_val) if rating_val is not None else None

                    author_info = item.get("author")
                    author = None
                    if isinstance(author_info, dict):
                        author = author_info.get("name")
                    elif isinstance(author_info, str):
                        author = author_info

                    review_date = None
                    date_str = item.get("date_created")
                    if date_str:
                        try:
                            review_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            pass

                    reviews.append(
                        RawReview(
                            source="2gis",
                            text=text[:500],
                            rating=rating,
                            author=author,
                            review_date=review_date,
                            source_link=source_link,
                        )
                    )

                log.info(
                    "twogis.reviews.native_api.done",
                    source_id=source_id,
                    count=len(reviews),
                )
                return reviews[:max_reviews]

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError) as exc:
                log.warning(
                    "twogis.reviews.native_api.network_error",
                    source_id=source_id,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                continue
            except Exception as exc:
                log.warning(
                    "twogis.reviews.native_api.error",
                    source_id=source_id,
                    error=str(exc),
                )
                return []

        return reviews

    async def _fetch_flamp_fallback(self, source_id: str, company_name: str) -> list[RawReview]:
        """Try fetching reviews from Flamp using the 2GIS firm ID."""
        try:
            parser = _get_flamp_parser()
            flamp_url = f"https://omsk.flamp.ru/firm/{source_id}"
            reviews = await parser.safe_parse(flamp_url, company_name)
            max_reviews = max(1, int(settings.flamp_max_reviews_per_company))
            reviews = reviews[:max_reviews]
            # Re-tag reviews as 2gis source for consistency in the pipeline
            for review in reviews:
                review.source = "2gis"
            log.info(
                "twogis.flamp_fallback.result",
                source_id=source_id,
                reviews_collected=len(reviews),
                max_reviews=max_reviews,
            )
            return reviews
        except Exception as exc:
            log.warning("twogis.flamp_fallback.error", source_id=source_id, error=str(exc))
            return []


def _normalize_search_base(url: str, *, fallback: str) -> str:
    base = url or fallback
    if "/page/" in base:
        base = re.sub(r"/page/\d+$", "", base)
    return base or fallback


def _extract_page_number(url: str) -> int:
    match = re.search(r"/page/(\d+)", url or "")
    return int(match.group(1)) if match else 1


def _is_forbidden_html(html: str) -> bool:
    low = (html or "").lower()
    if "forbidden" not in low:
        return False
    return (
        "if you are not a bot" in low
        or "origin: https://2gis.ru" in low
        or "support team" in low
        or "copy the report" in low
    )


async def _detect_max_search_page(page) -> int:
    try:
        candidates = await page.evaluate(
            """() => {
                const out = [];
                for (const a of document.querySelectorAll('a[href*="/search/"]')) {
                    const href = a.href ? a.href.split('?')[0] : '';
                    const txt = (a.textContent || '').trim();
                    out.push([href, txt]);
                }
                return out;
            }"""
        )
    except Exception:
        return 0

    max_page = 1
    for href, txt in candidates:
        href_str = str(href or "")
        txt_str = str(txt or "").strip()
        match = re.search(r"/page/(\d+)", href_str)
        if match:
            max_page = max(max_page, int(match.group(1)))
        if txt_str.isdigit():
            max_page = max(max_page, int(txt_str))
    return max_page


def _extract_firm_id(url: str) -> str | None:
    match = re.search(r"/firm/(\d+)", url)
    return match.group(1) if match else None


def _text(el) -> str | None:
    if not el:
        return None
    txt = el.get_text(" ", strip=True)
    return txt or None


def _extract_title_fallback(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return str(meta["content"]).strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def _extract_phones(soup: BeautifulSoup) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()
    for a in soup.select('a[href^="tel:"]'):
        href = a.get("href", "").replace("tel:", "").strip()
        cleaned = re.sub(r"[^\d+]", "", href)
        if cleaned.startswith("8") and len(cleaned) == 11:
            cleaned = "+7" + cleaned[1:]
        if cleaned and sum(ch.isdigit() for ch in cleaned) >= 10 and cleaned not in seen:
            seen.add(cleaned)
            phones.append(cleaned)
    return phones


def _extract_addresses(soup: BeautifulSoup) -> list[str]:
    addrs: list[str] = []
    seen: set[str] = set()

    for item in _extract_ld_json_items(soup):
        addr = item.get("address")
        if isinstance(addr, dict):
            parts = [
                str(addr.get("addressLocality") or "").strip(),
                str(addr.get("streetAddress") or "").strip(),
            ]
            merged = ", ".join([p for p in parts if p])
            _append_unique_address(addrs, seen, merged)

    for a in soup.select('a[href*="/geo/"]'):
        street = _text(a)
        if not street:
            continue

        extra = None
        span = a.find_parent("span")
        if span:
            sibling = span.find_next_sibling("div")
            extra = _text(sibling)

        merged = f"{street}, {extra}" if extra else street
        _append_unique_address(addrs, seen, merged)

    candidates = []
    candidates.extend(soup.select('a[href*="/geo/"]'))
    candidates.extend(soup.select('[itemprop="streetAddress"]'))
    candidates.extend(soup.select('[class*="address"]'))

    for el in candidates:
        txt = _text(el)
        if not txt:
            continue
        _append_unique_address(addrs, seen, txt)
    return addrs[:3]


def _append_unique_address(addrs: list[str], seen: set[str], value: str | None) -> None:
    if not value:
        return
    txt = " ".join(str(value).replace("\xa0", " ").split()).strip(" ,")
    if not txt or len(txt) < 6:
        return
    lower = txt.lower()
    if "показать вход" in lower:
        return
    if txt not in seen:
        seen.add(txt)
        addrs.append(txt)


def _extract_websites(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href.startswith("http"):
            continue
        if "2gis.ru" in href:
            continue
        norm = href.split("?")[0]
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


_INN_PATTERNS = [
    re.compile(r"(?:\"inn\"|\"taxid\"|\"tax_id\")\s*[:=]\s*\"?(\d{10}|\d{12})\"?", re.IGNORECASE),
    re.compile(r"(?:ИНН|inn)\D{0,12}(\d{10}|\d{12})", re.IGNORECASE),
]

_OGRN_PATTERNS = [
    re.compile(r"(?:\"ogrn\"|\"ogrnip\"|\"ogrn_ip\")\s*[:=]\s*\"?(\d{13}|\d{15})\"?", re.IGNORECASE),
    re.compile(r"(?:ОГРНИП|ОГРН|ogrn)\D{0,12}(\d{13}|\d{15})", re.IGNORECASE),
]


def _extract_tax_ids(soup: BeautifulSoup, html: str) -> tuple[str | None, str | None]:
    search_space = [soup.get_text(" ", strip=True), html or ""]

    # Structured data first: less noisy than raw HTML text scans.
    for item in _extract_ld_json_items(soup):
        for key in ("taxID", "tax_id", "inn", "INN"):
            value = item.get(key)
            if isinstance(value, str):
                m = re.fullmatch(r"\d{10}|\d{12}", value.strip())
                if m:
                    inn = m.group(0)
                    ogrn = _extract_tax_id_from_strings(search_space, _OGRN_PATTERNS)
                    return inn, ogrn

    inn = _extract_tax_id_from_strings(search_space, _INN_PATTERNS)
    ogrn = _extract_tax_id_from_strings(search_space, _OGRN_PATTERNS)
    return inn, ogrn


def _extract_tax_id_from_strings(search_space: list[str], patterns: list[re.Pattern[str]]) -> str | None:
    for text in search_space:
        if not text:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return None


def _extract_rating_info(soup: BeautifulSoup) -> tuple[float | None, int | None]:
    for item in _extract_ld_json_items(soup):
        aggregate_rating = item.get("aggregateRating")
        if not isinstance(aggregate_rating, dict):
            continue
        rating = parse_float(str(aggregate_rating.get("ratingValue", "")))
        count = parse_int(str(aggregate_rating.get("reviewCount", "")))
        if rating or count:
            return rating, count

    text = soup.get_text(" ", strip=True)
    rating = None
    count = None

    rating_match = re.search(r"([1-5][\.,]\d)\s*(?:из|/)\s*5", text, flags=re.IGNORECASE)
    if rating_match:
        rating = parse_float(rating_match.group(1))

    count_match = re.search(r"(?<!\d)(\d{1,6})\s+отзыв", text, flags=re.IGNORECASE)
    if count_match:
        count = parse_int(count_match.group(1))

    return rating, count


_REVIEW_API_KEY_PATTERNS = [
    re.compile(r'reviewApiKey["\s:=]+["\']([a-z0-9]{20,})["\']', re.IGNORECASE),
    re.compile(r'"reviewApiKey"\s*:\s*"([a-z0-9]{20,})"', re.IGNORECASE),
    re.compile(r'REVIEW_API_KEY["\s:=]+["\']([a-z0-9]{20,})["\']', re.IGNORECASE),
    re.compile(r'key["\s:=]+["\']([a-z0-9]{32,})["\'].*?review', re.IGNORECASE),
]


def _extract_review_api_key(html: str) -> str | None:
    """Extract the reviewApiKey from 2GIS page HTML using multiple regex patterns."""
    if not html:
        return None
    for pattern in _REVIEW_API_KEY_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def _extract_reviews_from_soup(soup: BeautifulSoup, *, source_link: str) -> list[RawReview]:
    reviews: list[RawReview] = []
    seen: set[str] = set()

    selectors = [
        "[itemprop='review']",
        ".reviewsItem",
        "[class*='review-item']",
        "[class*='ReviewSnippet']",
    ]

    for selector in selectors:
        for block in soup.select(selector):
            text = _text(block)
            if not text or len(text) < 20:
                continue
            key = text.strip().lower()[:160]
            if key in seen:
                continue
            seen.add(key)
            reviews.append(
                RawReview(
                    source="2gis",
                    text=text[:500],
                    rating=None,
                    author=None,
                    source_link=source_link,
                )
            )
        if reviews:
            break

    return reviews


def _extract_reviews_from_initial_state(
    html: str,
    *,
    source_id: str | None,
    source_link: str,
    max_reviews: int = 50,
) -> list[RawReview]:
    state = _extract_initial_state(html)
    if not state:
        return []

    bucket = ((state.get("data") or {}).get("review") or {})
    if not isinstance(bucket, dict):
        return []

    reviews: list[RawReview] = []
    seen: set[str] = set()

    for wrapped in bucket.values():
        if not isinstance(wrapped, dict):
            continue
        data = wrapped.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("is_hidden") is True:
            continue

        obj = data.get("object")
        if source_id and isinstance(obj, dict):
            obj_id = str(obj.get("id") or "")
            if obj_id and obj_id != str(source_id):
                continue

        text_raw = data.get("text")
        if not isinstance(text_raw, str):
            continue
        text = _normalise_review_text(text_raw)
        if not text or len(text) < 10:
            continue

        dedup_key = text.lower()[:160]
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        rating = parse_float(str(data.get("rating", "")))

        author = None
        user = data.get("user")
        if isinstance(user, dict):
            author_raw = user.get("name")
            if isinstance(author_raw, str):
                author = _normalise_review_text(author_raw)

        review_date = None
        date_str = data.get("date_created")
        if isinstance(date_str, str) and date_str:
            try:
                review_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                review_date = None

        reviews.append(
            RawReview(
                source="2gis",
                text=text[:500],
                rating=rating,
                author=author,
                review_date=review_date,
                source_link=source_link,
            )
        )

        if len(reviews) >= max_reviews:
            break

    return reviews


def _extract_initial_state(html: str) -> dict | None:
    if not html:
        return None
    match = re.search(r"var\s+initialState\s*=\s*JSON\.parse\('(.+?)'\);", html, flags=re.S)
    if not match:
        return None
    raw = match.group(1)
    try:
        unescaped = raw.encode("utf-8").decode("unicode_escape")
        state = json.loads(unescaped)
        if isinstance(state, dict):
            return state
    except Exception:
        return None
    return None


def _normalise_review_text(text: str | None) -> str:
    if not text:
        return ""
    txt = " ".join(str(text).replace("\xa0", " ").split()).strip()
    if not txt:
        return ""
    # Some 2GIS initialState payloads contain UTF-8 bytes decoded as latin1.
    for _ in range(3):
        if not any(marker in txt for marker in ("Ð", "Ñ", "Ã", "Â")):
            break
        try:
            fixed = txt.encode("latin1", errors="ignore").decode("utf-8", errors="ignore").strip()
        except Exception:
            break
        if not fixed or fixed == txt:
            break
        txt = fixed
    return txt


def _extract_ld_json_items(soup: BeautifulSoup) -> list[dict]:
    items: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        chunk = data if isinstance(data, list) else [data]
        for item in chunk:
            if isinstance(item, dict):
                items.append(item)
    return items
