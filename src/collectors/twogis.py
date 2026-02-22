"""2GIS scraper — primary: Playwright UI with pagination & parallel scraping, fallback: captured API key."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

CATALOG_HOST = "catalog.api.2gis.com"
CATALOG_URL = f"https://{CATALOG_HOST}/3.0/items"
REVIEWS_URL = "https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"
_HOME_PAGE = "https://2gis.ru/omsk"
_SEARCH_URL = "https://2gis.ru/omsk/search/{query}"

# Крупные населённые пункты Омской области (from Denis — regional search)
_REGION_LOCALITIES = [
    "Тара",
    "Исилькуль",
    "Калачинск",
    "Называевск",
    "Тюкалинск",
    "Таврическое",
    "Любинский",
    "Москаленки",
    "Марьяновка",
    "Кормиловка",
    "Нижняя Омка",
    "Большеречье",
]


class TwoGisCollector(AbstractCollector):
    source_name = "2gis"
    _max_concurrent_keywords = 2  # Playwright is heavy

    # Class-level cache — key is extracted once and reused across keywords
    _api_key: str | None = None

    # ------------------------------------------------------------------ public

    async def collect(self, keyword: str) -> list[RawCompany]:
        log.info("twogis.collect.start", keyword=keyword)

        all_companies: list[RawCompany] = []
        seen_ids: set[str] = set()

        def _dedup_extend(companies: list[RawCompany]) -> int:
            added = 0
            for c in companies:
                if c.source_id and c.source_id in seen_ids:
                    continue
                if c.source_id:
                    seen_ids.add(c.source_id)
                all_companies.append(c)
                added += 1
            return added

        # 1. Primary: UI scraping with pagination + parallel firm pages
        companies = await self._collect_via_ui(keyword)
        if not companies:
            # Fallback: API key
            api_key = await self._ensure_api_key()
            if api_key:
                companies = await self._fetch_items(keyword, api_key)
        _dedup_extend(companies)

        # 2. Regional: search with locality names (from Denis)
        if settings.twogis_search_region:
            for locality in _REGION_LOCALITIES:
                regional_kw = f"{keyword} {locality}"
                regional = await self._collect_via_ui(regional_kw)
                added = _dedup_extend(regional)
                if added:
                    log.info("twogis.collect.region", keyword=keyword, locality=locality, added=added)

        log.info("twogis.collect.done", keyword=keyword, count=len(all_companies),
                 regional=settings.twogis_search_region)
        return all_companies

    # ----------------------------------------------------------------- UI scraping

    async def _collect_via_ui(self, keyword: str) -> list[RawCompany]:
        query = quote_plus(keyword)
        search_url = _SEARCH_URL.format(query=query)

        # Try proxy rotation until success, then direct.
        if proxy_manager.count > 0:
            use_proxy_attempts = min(proxy_manager.count, max(1, settings.twogis_max_proxy_rotations))
        else:
            use_proxy_attempts = 0

        for attempt in range(1, use_proxy_attempts + 1):
            companies = await self._collect_via_ui_once(keyword, search_url, use_proxy=True)
            if companies:
                return companies
            log.info("twogis.ui.proxy_retry", attempt=attempt + 1, max_attempts=use_proxy_attempts)
        return await self._collect_via_ui_once(keyword, search_url, use_proxy=False)

    async def _collect_via_ui_once(
        self,
        keyword: str,
        search_url: str,
        *,
        use_proxy: bool,
    ) -> list[RawCompany]:
        async with async_playwright() as pw:
            proxy = proxy_manager.playwright_proxy() if use_proxy else None
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
                    log.warning("twogis.ui.goto_error", error=str(exc), use_proxy=use_proxy)
                    return []
                if resp and resp.status in (401, 403, 429, 503):
                    log.warning("twogis.ui.blocked_status", status=resp.status, use_proxy=use_proxy)
                    return []

                if "captcha.2gis.ru" in page.url:
                    log.warning("twogis.ui.captcha", use_proxy=use_proxy)
                    return []
                first_html = await page.content()
                if _is_forbidden_html(first_html):
                    log.warning("twogis.ui.forbidden_page", use_proxy=use_proxy)
                    return []

                search_pages = await self._collect_search_pages(page, search_url)
                log.info("twogis.ui.pages_collected", count=len(search_pages), use_proxy=use_proxy)

                firm_urls = await self._collect_firm_urls_from_pages(page, search_pages)
                if not firm_urls:
                    log.info("twogis.ui.no_firms", use_proxy=use_proxy)
                    return []

                return await self._scrape_firm_pages_parallel(
                    pw,
                    firm_urls[:120],
                    keyword,
                    use_proxy=use_proxy,
                )
            finally:
                await browser.close()

    async def _collect_search_pages(self, page, search_url: str) -> list[str]:
        """Build deterministic /page/N list, preferring real page count from UI."""
        base = _normalize_search_base(page.url.split("?")[0], fallback=search_url.split("?")[0])
        max_page = await _detect_max_search_page(page)
        if max_page <= 0:
            max_page = max(1, settings.twogis_search_max_pages)
        else:
            max_page = min(max_page, max(1, settings.twogis_search_max_pages))
        return [base] + [f"{base}/page/{idx}" for idx in range(2, max_page + 1)]

    async def _collect_firm_urls_from_pages(self, page, page_urls: Iterable[str]) -> list[str]:
        """Visit each search page, scroll results and collect unique /firm/{id} links."""
        urls: list[str] = []
        seen: set[str] = set()

        for idx, page_url in enumerate(page_urls, start=1):
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
            # If /page/N redirects back to page 1, pagination ended -> stop.
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
                for u in found:
                    if u not in seen:
                        seen.add(u)
                        urls.append(u)
                        added += 1

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

        return urls

    # ----------------------------------------------------------------- parallel firm scraping

    async def _scrape_firm_pages_parallel(
        self,
        pw,
        firm_urls: list[str],
        keyword: str,
        *,
        use_proxy: bool,
    ) -> list[RawCompany]:
        if not firm_urls:
            return []

        workers = max(1, settings.twogis_firm_workers)
        workers = min(workers, len(firm_urls))
        if use_proxy and proxy_manager.count > 0:
            workers = min(workers, proxy_manager.count)

        batches = [firm_urls[i::workers] for i in range(workers)]
        max_retry_attempts = max(1, settings.twogis_company_retry_attempts)
        tabs_per_browser = max(1, settings.twogis_tabs_per_browser)
        tasks = [
            self._scrape_firm_batch(
                pw,
                batch,
                keyword,
                use_proxy=use_proxy,
                worker_idx=idx + 1,
                tabs_per_browser=tabs_per_browser,
                max_retry_attempts=max_retry_attempts,
            )
            for idx, batch in enumerate(batches)
            if batch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        out: list[RawCompany] = []
        for res in results:
            if isinstance(res, Exception):
                log.warning("twogis.ui.batch_error", error=str(res))
                continue
            out.extend(res)
        return out

    async def _scrape_firm_batch(
        self,
        pw,
        firm_urls: list[str],
        keyword: str,
        *,
        use_proxy: bool,
        worker_idx: int,
        tabs_per_browser: int,
        max_retry_attempts: int,
    ) -> list[RawCompany]:
        headless = not settings.twogis_debug_browser
        pending_urls = list(firm_urls)
        out: list[RawCompany] = []

        total_attempts = max_retry_attempts + (1 if use_proxy else 0)
        if use_proxy and settings.twogis_retry_until_success:
            total_attempts = max(
                total_attempts,
                max_retry_attempts + max(1, settings.twogis_max_proxy_rotations),
            )

        proxy = None
        for attempt in range(1, total_attempts + 1):
            if not pending_urls:
                break

            is_direct_fallback = use_proxy and attempt > max_retry_attempts
            proxy = None if is_direct_fallback else (proxy_manager.playwright_proxy() if use_proxy else None)
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
                    attempt=attempt + 1,
                    pending=len(pending_urls),
                    proxy_mode="direct" if is_direct_fallback else "proxy",
                    proxy=proxy.get("server") if proxy else None,
                )
                await asyncio.sleep(0.5)

        log.info(
            "twogis.ui.batch_done",
            worker=worker_idx,
            use_proxy=use_proxy,
            proxy=proxy.get("server") if proxy else None,
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
        """Return (company, retry_needed)."""
        page = await context.new_page()
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
        reviews = await self._fetch_ui_reviews(source_id, html)
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
            average_rating=average_rating,
            reviews_count=reviews_count,
            reviews=reviews,
            contacts_json={"websites": websites},
            raw_payload={"keyword": keyword, "mode": "ui"},
        )
        return company, False

    async def _scrape_firm_page(self, context, url: str, keyword: str) -> RawCompany | None:
        company, _ = await self._scrape_firm_page_status(context, url, keyword)
        return company

    async def _fetch_ui_reviews(self, source_id: str | None, page_html: str) -> list[RawReview]:
        """Fetch review texts in UI mode using reviewApiKey embedded in page HTML."""
        if not source_id:
            return []
        api_key = _extract_review_api_key_from_html(page_html)
        if not api_key:
            return []

        params = {"key": api_key, "page_size": 50, "is_advertiser": "false"}
        url = REVIEWS_URL.format(branch_id=source_id)
        # Try with proxy first, fallback to direct if 407/proxy error
        proxy_url = proxy_manager.get_next()
        data = None
        for attempt_proxy in (proxy_url, None):
            try:
                async with httpx.AsyncClient(timeout=20.0, proxy=attempt_proxy or None) as client:
                    resp = await client.get(url, params=params)
                if resp.status_code == 407:
                    # Proxy auth failed — retry without proxy
                    continue
                if resp.status_code != 200:
                    return []
                data = resp.json()
                break
            except Exception as exc:
                if attempt_proxy:
                    # Proxy failed — try direct
                    continue
                log.debug("twogis.ui.reviews_error", source_id=source_id, error=str(exc))
                return []
        if data is None:
            return []

        out: list[RawReview] = []
        for r in data.get("reviews", []):
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            rating = parse_float(str(r.get("rating", "")))
            out.append(
                RawReview(
                    source=self.source_name,
                    text=text,
                    rating=rating,
                    author=r.get("user", {}).get("name"),
                    source_link=f"https://2gis.ru/omsk/firm/{source_id}/tab/reviews",
                )
            )
        return out

    # ----------------------------------------------------------------- key capture (fallback)

    async def _ensure_api_key(self) -> str | None:
        if settings.twogis_api_key:
            return settings.twogis_api_key
        if TwoGisCollector._api_key:
            return TwoGisCollector._api_key

        key = await self._capture_api_key()
        if key:
            TwoGisCollector._api_key = key
            log.info("twogis.api_key_captured", key=key[:8] + "***")
        return key

    async def _capture_api_key(self) -> str | None:
        """Open 2GIS and obtain API key from requests or embedded page state."""
        if proxy_manager.count > 0:
            proxy_attempts = min(3, proxy_manager.count)
            for attempt in range(1, proxy_attempts + 1):
                key = await self._capture_api_key_once(use_proxy=True)
                if key:
                    return key
                log.debug("twogis.key_capture.proxy_attempt_failed", attempt=attempt)

        key = await self._capture_api_key_once(use_proxy=False)
        if key:
            return key
        return None

    async def _capture_api_key_once(self, *, use_proxy: bool) -> str | None:
        captured: list[str] = []

        async with async_playwright() as pw:
            proxy = proxy_manager.playwright_proxy() if use_proxy else None
            if use_proxy:
                log.debug("twogis.key_capture.proxy_pick", proxy=proxy.get("server") if proxy else None)
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
                    )
                )
                page = await context.new_page()

                def _on_request(request):
                    if CATALOG_HOST in request.url and not captured:
                        params = parse_qs(urlparse(request.url).query)
                        if "key" in params:
                            captured.append(params["key"][0])

                page.on("request", _on_request)

                try:
                    await page.goto(_HOME_PAGE, wait_until="domcontentloaded", timeout=45_000)
                    await asyncio.sleep(3)
                except Exception as exc:
                    log.warning("twogis.key_capture.error", error=str(exc), use_proxy=use_proxy)

                if captured:
                    for candidate in captured:
                        if await self._is_api_key_valid(candidate):
                            return candidate
                        log.debug("twogis.api_key_invalid_candidate", source="request")

                # Fallback: 2GIS often embeds the key in inline JSON/config.
                try:
                    html = await page.content()
                    embedded = _extract_api_key_from_html(html)
                    if embedded:
                        if await self._is_api_key_valid(embedded):
                            log.info("twogis.api_key_embedded_found", use_proxy=use_proxy)
                            return embedded
                        log.debug("twogis.api_key_invalid_candidate", source="embedded")
                except Exception as exc:
                    log.debug("twogis.key_capture.content_error", error=str(exc), use_proxy=use_proxy)
            finally:
                await browser.close()

        return None

    async def _is_api_key_valid(self, key: str) -> bool:
        """Verify key against 2GIS catalog API."""
        params = {
            "q": "сантехник",
            "region_id": settings.twogis_region_id,
            "key": key,
            "page_size": 1,
            "type": "branch",
        }
        proxy_url = proxy_manager.get_next()
        try:
            async with httpx.AsyncClient(timeout=12.0, proxy=proxy_url or None) as client:
                resp = await client.get(CATALOG_URL, params=params)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            meta_code = data.get("meta", {}).get("code")
            return resp.status_code == 200 and meta_code not in (401, 403)
        except Exception:
            return False

    # ----------------------------------------------------------------- API data fetching (fallback)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_items(self, keyword: str, api_key: str) -> list[RawCompany]:
        proxy_url = proxy_manager.get_next()
        if proxy_url:
            log.debug("twogis.catalog.proxy_pick", proxy=proxy_url)
        async with httpx.AsyncClient(timeout=30.0, proxy=proxy_url or None) as client:
            params = {
                "q": keyword,
                "region_id": settings.twogis_region_id,
                "key": api_key,
                "fields": "items.point,items.address,items.contact_groups,items.rubrics,items.reviews",
                "page_size": 50,
                "type": "branch",
            }

            all_companies: list[RawCompany] = []
            page = 1

            while True:
                params["page"] = page
                resp = await client.get(CATALOG_URL, params=params)

                if resp.status_code in (401, 403):
                    TwoGisCollector._api_key = None
                    log.warning("twogis.api_key_expired", status=resp.status_code)
                    return []

                resp.raise_for_status()
                data = resp.json()
                meta_code = data.get("meta", {}).get("code")
                if meta_code in (401, 403):
                    TwoGisCollector._api_key = None
                    log.warning("twogis.api_key_invalid", meta_code=meta_code)
                    return []

                items = data.get("result", {}).get("items", [])
                if not items:
                    break

                for item in items:
                    company = self._parse_item(item, keyword)
                    if company:
                        if item.get("id"):
                            reviews = await self._fetch_reviews(item["id"], api_key, client)
                            company.reviews = reviews
                            if reviews:
                                ratings = [r.rating for r in reviews if r.rating is not None]
                                if ratings:
                                    company.average_rating = round(sum(ratings) / len(ratings), 2)
                                company.reviews_count = len(reviews)
                        all_companies.append(company)

                total = data.get("result", {}).get("total", 0)
                if page * 50 >= total or page >= 10:
                    break
                page += 1
                await asyncio.sleep(0.5)

        return all_companies

    def _parse_item(self, item: dict, keyword: str) -> RawCompany | None:
        name_raw = item.get("name_ex", {}).get("primary") or item.get("name")
        if not name_raw:
            return None

        phones: list[str] = []
        for cg in item.get("contact_groups", []):
            for contact in cg.get("contacts", []):
                if contact.get("type") == "phone":
                    val = contact.get("value", "")
                    if val:
                        phones.append(val)

        addresses: list[str] = []
        addr = item.get("address", {})
        if addr:
            full_addr = addr.get("name") or (addr.get("components") or [{}])[0].get("street_address")
            if full_addr:
                addresses.append(full_addr)

        point = item.get("point", {})
        reviews_info = item.get("reviews", {})
        average_rating = reviews_info.get("rating")
        reviews_count = reviews_info.get("count")
        source_link = f"https://2gis.ru/omsk/firms/{item.get('id', '')}"

        return RawCompany(
            source=self.source_name,
            source_id=str(item.get("id", "")),
            source_link=source_link,
            name_raw=name_raw,
            phones=phones,
            addresses=addresses,
            average_rating=float(average_rating) if average_rating else None,
            reviews_count=int(reviews_count) if reviews_count else None,
            raw_payload={
                "keyword": keyword,
                "point": point,
                "rubrics": item.get("rubrics", []),
                "item_id": item.get("id"),
            },
        )

    async def _fetch_reviews(
        self,
        branch_id: str,
        api_key: str,
        client: httpx.AsyncClient,
    ) -> list[RawReview]:
        url = REVIEWS_URL.format(branch_id=branch_id)
        params = {"key": api_key, "page_size": 50, "is_advertiser": "false"}
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            log.warning("twogis.reviews.error", branch_id=branch_id, error=str(exc))
            return []

        reviews: list[RawReview] = []
        for r in data.get("reviews", []):
            reviews.append(
                RawReview(
                    source=self.source_name,
                    text=r.get("text"),
                    rating=float(r.get("rating", 0)) or None,
                    author=r.get("user", {}).get("name"),
                    source_link=f"https://2gis.ru/omsk/firms/{branch_id}",
                )
            )
        return reviews


# ── HTML parsing helpers ─────────────────────────────────────────────────────


def _extract_api_key_from_html(html: str) -> str | None:
    patterns = [
        r'"apiKey"\s*:\s*"([^"]+)"',
        r'"key"\s*:\s*"([a-zA-Z0-9._-]{20,})"',
        r'key=([a-zA-Z0-9._-]{20,})',
    ]
    for pattern in patterns:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None


def _normalize_search_base(url: str, *, fallback: str) -> str:
    base = url or fallback
    if "/page/" in base:
        base = re.sub(r"/page/\d+$", "", base)
    return base or fallback


def _extract_page_number(url: str) -> int:
    m = re.search(r"/page/(\d+)", url or "")
    return int(m.group(1)) if m else 1


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
    """Try to read total number of search result pages from pagination controls."""
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

        m_href = re.search(r"/page/(\d+)", href_str)
        if m_href:
            max_page = max(max_page, int(m_href.group(1)))
        if txt_str.isdigit():
            max_page = max(max_page, int(txt_str))
    return max_page


def _extract_review_api_key_from_html(html: str) -> str | None:
    m = re.search(r'"reviewApiKey"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def _extract_firm_id(url: str) -> str | None:
    m = re.search(r"/firm/(\d+)", url)
    return m.group(1) if m else None


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

    # JSON-LD is the most stable source for address fields.
    for item in _extract_ld_json_items(soup):
        addr = item.get("address")
        if isinstance(addr, dict):
            parts = [
                str(addr.get("addressLocality") or "").strip(),
                str(addr.get("streetAddress") or "").strip(),
            ]
            merged = ", ".join([p for p in parts if p])
            _append_unique_address(addrs, seen, merged)

    # 2GIS UI block: street link + district/city/index in sibling div (from Andrey).
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
    if not txt:
        return
    if len(txt) < 6:
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


def _extract_rating_info(soup: BeautifulSoup) -> tuple[float | None, int | None]:
    # 1) Try JSON-LD aggregateRating (most reliable).
    for item in _extract_ld_json_items(soup):
        ar = item.get("aggregateRating")
        if not isinstance(ar, dict):
            continue
        rating = parse_float(str(ar.get("ratingValue", "")))
        count = parse_int(str(ar.get("reviewCount", "")))
        if rating or count:
            return rating, count

    # 2) Try meta tags (from Denis).
    meta_rating = soup.find("meta", attrs={"itemprop": "ratingValue"})
    meta_count = soup.find("meta", attrs={"itemprop": "reviewCount"})
    if meta_rating or meta_count:
        rating = parse_float(meta_rating["content"]) if meta_rating and meta_rating.get("content") else None
        count = parse_int(meta_count["content"]) if meta_count and meta_count.get("content") else None
        if rating or count:
            return rating, count

    # 3) Fallback from page text (targeted patterns only, from Andrey).
    text = soup.get_text(" ", strip=True)
    rating = None
    count = None

    m_rating = re.search(r"([1-5][\.,]\d)\s*(?:из|/)\s*5", text, flags=re.IGNORECASE)
    if m_rating:
        rating = parse_float(m_rating.group(1))

    m_count = re.search(r"(?<!\d)(\d{1,6})\s+отзыв", text, flags=re.IGNORECASE)
    if m_count:
        count = parse_int(m_count.group(1))

    return rating, count


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
