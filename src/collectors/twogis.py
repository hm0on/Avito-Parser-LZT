"""2GIS scraper — primary: Playwright UI scraping, fallback: captured API key."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, quote_plus, urlparse

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

# Крупные населённые пункты Омской области (добавляются к запросу для расширения поиска)
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

        # 1. Primary: search in Omsk city
        companies = await self._collect_via_ui(keyword)
        if not companies:
            api_key = await self._ensure_api_key()
            if api_key:
                companies = await self._fetch_items(keyword, api_key)
        _dedup_extend(companies)

        # 2. Regional: search with locality names appended to the keyword
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

        # Try proxies first, then direct.
        use_proxy_attempts = min(3, proxy_manager.count) if proxy_manager.count > 0 else 0
        for _ in range(use_proxy_attempts):
            companies = await self._collect_via_ui_once(keyword, search_url, use_proxy=True)
            if companies:
                return companies
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
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy,
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
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
                    await asyncio.sleep(2)
                except Exception as exc:
                    log.warning("twogis.ui.goto_error", error=str(exc), use_proxy=use_proxy)
                    return []

                if "captcha.2gis.ru" in page.url:
                    log.warning("twogis.ui.captcha", use_proxy=use_proxy)
                    return []

                firm_urls = await self._collect_firm_urls(page)
                if not firm_urls:
                    log.info("twogis.ui.no_firms", use_proxy=use_proxy)
                    return []

                out: list[RawCompany] = []
                for url in firm_urls[:120]:
                    company = await self._scrape_firm_page(context, url, keyword)
                    if company:
                        out.append(company)
                    await asyncio.sleep(0.2)
                return out
            finally:
                await browser.close()

    async def _collect_firm_urls(self, page) -> list[str]:
        """Scroll search page and collect unique /firm/{id} links."""
        urls: list[str] = []
        seen: set[str] = set()
        stable_rounds = 0

        for _ in range(30):
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
            await asyncio.sleep(0.8)

            if added == 0:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 4:
                break

        return urls

    async def _scrape_firm_page(self, context, url: str, keyword: str) -> RawCompany | None:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            await asyncio.sleep(0.7)
            if "captcha.2gis.ru" in page.url:
                return None
            html = await page.content()
        except Exception:
            return None
        finally:
            await page.close()

        soup = BeautifulSoup(html, "html.parser")
        name = _text(soup.select_one("h1")) or _extract_title_fallback(soup)
        if not name:
            return None

        source_id = _extract_firm_id(url)
        phones = _extract_phones(soup)
        addresses = _extract_addresses(soup)
        average_rating, reviews_count = _extract_rating_info(soup)
        websites = _extract_websites(soup)

        return RawCompany(
            source=self.source_name,
            source_id=source_id,
            source_link=url,
            name_raw=name,
            phones=phones,
            addresses=addresses,
            average_rating=average_rating,
            reviews_count=reviews_count,
            contacts_json={"websites": websites},
            raw_payload={"keyword": keyword, "mode": "ui"},
        )

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
        return key

    async def _capture_api_key_once(self, *, use_proxy: bool) -> str | None:
        captured: list[str] = []

        async with async_playwright() as pw:
            proxy = proxy_manager.playwright_proxy() if use_proxy else None
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy,
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

                # Fallback: 2GIS often embeds the key in inline JSON/config.
                try:
                    html = await page.content()
                    embedded = _extract_api_key_from_html(html)
                    if embedded and await self._is_api_key_valid(embedded):
                        log.info("twogis.api_key_embedded_found", use_proxy=use_proxy)
                        return embedded
                except Exception:
                    pass
            finally:
                await browser.close()

        return None

    async def _is_api_key_valid(self, key: str) -> bool:
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
            if merged and merged not in seen:
                seen.add(merged)
                addrs.append(merged)

    candidates = []
    candidates.extend(soup.select('a[href*="/geo/"]'))
    candidates.extend(soup.select('[itemprop="streetAddress"]'))
    candidates.extend(soup.select('[class*="address"]'))

    for el in candidates:
        txt = _text(el)
        if not txt or len(txt) < 6:
            continue
        if txt not in seen:
            seen.add(txt)
            addrs.append(txt)
    return addrs[:3]


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

    # 2) Try meta tags.
    meta_rating = soup.find("meta", attrs={"itemprop": "ratingValue"})
    meta_count = soup.find("meta", attrs={"itemprop": "reviewCount"})
    if meta_rating or meta_count:
        rating = parse_float(meta_rating["content"]) if meta_rating and meta_rating.get("content") else None
        count = parse_int(meta_count["content"]) if meta_count and meta_count.get("content") else None
        if rating or count:
            return rating, count

    # 3) Try rating-related elements by class/itemprop (targeted, not full page text).
    rating = None
    count = None
    for el in soup.select('[class*="rating"], [itemprop="ratingValue"]'):
        txt = _text(el)
        if txt and len(txt) < 20:
            r = parse_float(txt)
            if r is not None and 0 < r <= 5:
                rating = r
                break

    for el in soup.select('[class*="review"], [class*="comment"], [itemprop="reviewCount"]'):
        txt = _text(el)
        if txt and len(txt) < 30:
            c = parse_int(txt)
            if c is not None and 0 < c < 1_000_000:
                count = c
                break

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
