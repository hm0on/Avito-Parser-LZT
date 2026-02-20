"""Avito collector — httpx + BeautifulSoup HTML parsing.

Anti-block strategy:
  1. Residential proxy (PROXY_URL / PROXY_FILE)
  2. spfa.ru cookies (AVITO_COOKIES_API_KEY) — valid Avito session, ~12₽/set, 12h
  3. Auto-renewal: on 403 the provider requests unblock or buys fresh cookies

Data source: Avito renders listings as HTML with data-marker attributes.
Parser: BeautifulSoup with html.parser (lxml strips data-* attributes).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

AVITO_BASE = "https://www.avito.ru"
AVITO_SEARCH = f"{AVITO_BASE}/omsk/predlozheniya_uslug"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.google.ru/search?q=avito+%D0%BE%D0%BC%D1%81%D0%BA",
}

_BLOCK_MARKERS = ["доступ ограничен", "firewall-container", "captcha", "access denied"]


# ─────────────────────────────────────────────────────────────────────────────
# Cookie provider (spfa.ru)
# ─────────────────────────────────────────────────────────────────────────────

class _SpfaCookiesProvider:
    """Provides valid Avito session cookies via spfa.ru API.

    Pricing: ~12₽ per cookie set, lasts 12+ hours.
    Register at https://spfa.ru and get an API key.
    """

    _API = "https://spfa.ru/api"
    _STORAGE = Path("storage/avito_cookies.json")

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._id: str | None = None
        self._cookies: dict | None = None
        self._unblock_started_at: float | None = None
        self._UNBLOCK_TIMEOUT = 300
        self._load()

    def get(self) -> dict:
        return self._cookies or {}

    def fetch_phones(self, ad_ids: list[str]) -> dict[str, str]:
        """Fetch phone numbers for a batch of ad IDs via spfa.ru/api/phone/.

        Returns dict {ad_id: phone} for ads where phone was found.
        Processes in chunks of 50 (API limit).
        """
        import requests

        result: dict[str, str] = {}
        for i in range(0, len(ad_ids), 50):
            chunk = ad_ids[i : i + 50]
            try:
                r = requests.post(
                    f"{self._API}/phone/",
                    json={"api_key": self._api_key, "ads": chunk},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("success"):
                    for item in data.get("results", []):
                        phone = item.get("phone")
                        if phone:
                            result[str(item["ad_id"])] = phone
                    meta = data.get("meta", {})
                    log.info(
                        "avito.phones.batch_done",
                        chunk_size=len(chunk),
                        success=meta.get("success", 0),
                        time_sec=meta.get("time_sec"),
                    )
            except Exception as exc:
                log.warning("avito.phones.error", error=str(exc), chunk_start=i)
        return result

    def handle_block(self) -> None:
        if not self._id:
            self._buy()
            return
        import requests
        now = time.time()
        if self._unblock_started_at:
            if now - self._unblock_started_at < self._UNBLOCK_TIMEOUT:
                log.info("avito.cookies.waiting_unblock")
                return
            self._unblock_started_at = None
        try:
            r = requests.post(
                f"{self._API}/unblock/",
                json={"id": self._id, "api_key": self._api_key},
                timeout=15,
            )
            if r.status_code in (200, 202):
                self._unblock_started_at = now
                return
            if r.status_code == 409:
                self._unblock_started_at = self._unblock_started_at or now
                return
        except Exception as exc:
            log.warning("avito.cookies.unblock_error", error=str(exc))
        self._unblock_started_at = None
        self._buy()

    def _buy(self) -> None:
        import requests
        try:
            r = requests.post(
                f"{self._API}/cookies/",
                json={"api_key": self._api_key},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("results", {})
            self._id = data.get("id")
            self._cookies = data.get("cookies")
            self._save()
            log.info("avito.cookies.bought", id=self._id)
        except Exception as exc:
            log.warning("avito.cookies.buy_error", error=str(exc))

    def _load(self) -> None:
        if not self._STORAGE.exists():
            return
        try:
            d = json.loads(self._STORAGE.read_text(encoding="utf-8"))
            self._id = d.get("id")
            self._cookies = d.get("cookies")
            if self._id:
                log.info("avito.cookies.loaded", id=self._id)
        except Exception:
            pass

    def _save(self) -> None:
        self._STORAGE.parent.mkdir(parents=True, exist_ok=True)
        self._STORAGE.write_text(
            json.dumps({"id": self._id, "cookies": self._cookies, "saved_at": time.time()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _build_cookies_provider() -> _SpfaCookiesProvider | None:
    api_key = getattr(settings, "avito_cookies_api_key", "")
    if api_key:
        log.info("avito.cookies.spfa_enabled")
        return _SpfaCookiesProvider(api_key)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────

class AvitoCollector(AbstractCollector):
    source_name = "avito"

    def __init__(self) -> None:
        self._cookies_provider = _build_cookies_provider()

    async def enrich_phones(self, companies: list[RawCompany]) -> None:
        """Fetch phone numbers for collected companies via spfa.ru/api/phone/.

        Call this after collection (and optional limiting) to avoid wasting credits.
        """
        if not self._cookies_provider:
            return
        avito = [c for c in companies if c.source == "avito" and c.source_id]
        if not avito:
            return
        ad_ids = [c.source_id for c in avito]
        log.info("avito.phones.start", total=len(ad_ids))
        phones = await asyncio.to_thread(
            self._cookies_provider.fetch_phones, ad_ids
        )
        matched = 0
        for company in avito:
            phone = phones.get(company.source_id)
            if phone:
                company.phones = [phone]
                matched += 1
        log.info("avito.phones.done", matched=matched, total=len(avito))

    async def enrich_reviews(self, companies: list[RawCompany]) -> None:
        """Scrape seller reviews for Avito companies using Playwright.

        For companies with reviews_count > 0, navigates to the ad page,
        finds the seller's profile, and scrapes actual review texts.
        """
        candidates = [
            c for c in companies
            if c.source == "avito" and (c.reviews_count or 0) > 0 and c.source_link
        ]
        if not candidates:
            return

        log.info("avito.reviews.start", count=len(candidates))

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            proxy = proxy_manager.playwright_proxy()
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    user_agent=_HEADERS["User-Agent"],
                    locale="ru-RU",
                )

                # Set Avito cookies if available
                if self._cookies_provider:
                    cookies = self._cookies_provider.get()
                    if cookies:
                        await context.add_cookies([
                            {"name": k, "value": str(v), "domain": ".avito.ru", "path": "/"}
                            for k, v in cookies.items()
                        ])

                for company in candidates:
                    try:
                        reviews = await self._scrape_seller_reviews(context, company.source_link)
                        if reviews:
                            company.reviews = reviews
                            log.info(
                                "avito.reviews.scraped",
                                name=company.name_raw,
                                count=len(reviews),
                            )
                        else:
                            log.debug("avito.reviews.empty", name=company.name_raw)
                    except Exception as exc:
                        log.warning(
                            "avito.reviews.error",
                            name=company.name_raw,
                            error=str(exc),
                        )
                    await asyncio.sleep(1.5)  # Be polite to Avito
            finally:
                await browser.close()

        total_reviews = sum(len(c.reviews) for c in candidates)
        log.info("avito.reviews.done", total_reviews=total_reviews)

    async def _scrape_seller_reviews(self, context, source_link: str) -> list[RawReview]:
        """Navigate to ad → find seller profile → scrape reviews."""
        captured_reviews: list[RawReview] = []
        page = await context.new_page()

        # Intercept API responses that contain review data
        async def _on_response(response):
            try:
                url = response.url
                if response.status == 200 and ("rating" in url or "review" in url):
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type:
                        data = await response.json()
                        self._extract_reviews_from_json(data, source_link, captured_reviews)
            except Exception:
                pass

        page.on("response", _on_response)

        try:
            # Step 1: Visit ad page
            resp = await page.goto(source_link, wait_until="domcontentloaded", timeout=30_000)
            if not resp or resp.status >= 400:
                return []

            await page.wait_for_timeout(1500)

            # Step 2: Find seller profile link
            seller_href = None
            for selector in ['a[href*="/user/"]', 'a[data-marker*="seller"]']:
                el = await page.query_selector(selector)
                if el:
                    href = await el.get_attribute("href")
                    if href and "/user/" in href and "login" not in href:
                        seller_href = href
                        break

            if not seller_href:
                return []

            # Step 3: Navigate to seller profile
            if seller_href.startswith("/"):
                seller_href = f"{AVITO_BASE}{seller_href}"

            # Ensure we go to profile page
            if "/profile" not in seller_href:
                seller_href = seller_href.rstrip("/") + "/profile"

            await page.goto(seller_href, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(2000)

            # If API interception captured reviews, return them
            if captured_reviews:
                return captured_reviews

            # Step 4: Try to click reviews/ratings tab
            for tab_text in ["Отзывы", "отзыв", "Оценки", "оценк"]:
                try:
                    tab = page.get_by_text(tab_text, exact=False).first
                    if await tab.is_visible():
                        await tab.click()
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue

            # Check if API captured anything after clicking
            if captured_reviews:
                return captured_reviews

            # Fallback: extract from DOM
            return await self._extract_reviews_from_dom(page, source_link)
        finally:
            await page.close()

    def _extract_reviews_from_json(
        self, data: dict, source_link: str, out: list[RawReview]
    ) -> None:
        """Parse reviews from intercepted Avito API JSON response."""
        if not isinstance(data, dict):
            return

        # Try different JSON structures
        items = (
            data.get("reviews")
            or data.get("ratings")
            or data.get("items")
            or (data.get("result", {}).get("reviews") if isinstance(data.get("result"), dict) else None)
            or []
        )

        for item in items:
            if not isinstance(item, dict):
                continue

            text = (item.get("text") or item.get("body") or item.get("comment") or "").strip()
            if not text or len(text) < 10:
                continue

            score = item.get("score") or item.get("rating")
            author_data = item.get("sender") or item.get("author") or item.get("user")
            author = None
            if isinstance(author_data, dict):
                author = author_data.get("name") or author_data.get("public_name")
            elif isinstance(author_data, str):
                author = author_data

            out.append(
                RawReview(
                    source="avito",
                    text=text[:500],
                    rating=float(score) if score else None,
                    author=author,
                    review_date=None,
                    source_link=source_link,
                )
            )

    async def _extract_reviews_from_dom(self, page, source_link: str) -> list[RawReview]:
        """Fallback: extract reviews from visible DOM elements."""
        reviews = []

        # Try multiple selectors for review containers
        selectors = [
            '[data-marker*="review"]',
            '[data-marker*="rating"] li',
            '[class*="review"]',
            '[class*="rating-item"]',
        ]

        for selector in selectors:
            try:
                elements = await page.query_selector_all(selector)
                if not elements:
                    continue

                for el in elements[:50]:  # Limit to 50 reviews
                    try:
                        text = (await el.inner_text()).strip()
                    except Exception:
                        continue

                    if text and 20 < len(text) < 1500:
                        # Skip navigation/header elements
                        if any(skip in text.lower() for skip in ["войти", "зарегистрироваться", "avito", "продолжить"]):
                            continue

                        reviews.append(
                            RawReview(
                                source="avito",
                                text=text[:500],
                                rating=None,
                                author=None,
                                review_date=None,
                                source_link=source_link,
                            )
                        )

                if reviews:
                    break
            except Exception:
                continue

        return reviews

    async def collect(self, keyword: str) -> list[RawCompany]:
        url = f"{AVITO_SEARCH}?q={quote_plus(keyword)}"
        log.info("avito.collect.start", keyword=keyword, url=url)

        companies: list[RawCompany] = []

        for page in range(1, 6):  # max 5 pages × 50 items = 250
            page_url = f"{url}&p={page}" if page > 1 else url
            html_text = await self._fetch(page_url)

            if not html_text:
                break

            if _is_blocked(html_text):
                log.warning("avito.collect.blocked", page=page, keyword=keyword)
                if self._cookies_provider:
                    self._cookies_provider.handle_block()
                break

            page_companies = _parse_html(html_text, keyword)
            if not page_companies:
                log.info("avito.collect.empty_page", page=page, keyword=keyword)
                break

            companies.extend(page_companies)
            log.info(
                "avito.collect.page_done",
                page=page, found=len(page_companies), total=len(companies), keyword=keyword,
            )
            await asyncio.sleep(1.5)

        log.info("avito.collect.done", keyword=keyword, count=len(companies))
        return companies

    async def _fetch(self, url: str) -> str | None:
        """Fetch with up to 3 retries; on 429 renews cookies and waits before retry."""
        for attempt in range(1, 4):
            proxy_url = proxy_manager.get_next()
            cookies = self._cookies_provider.get() if self._cookies_provider else {}

            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url or None,
                    headers=_HEADERS,
                    cookies=cookies,
                    timeout=30.0,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url)

                    if resp.status_code == 429:
                        wait = 5 * attempt
                        log.warning("avito.fetch.rate_limited",
                                    attempt=attempt, wait_s=wait, url=url)
                        if self._cookies_provider:
                            self._cookies_provider.handle_block()
                        await asyncio.sleep(wait)
                        continue

                    if resp.status_code == 403:
                        log.warning("avito.fetch.forbidden", url=url)
                        if self._cookies_provider:
                            self._cookies_provider.handle_block()
                        return None

                    resp.raise_for_status()
                    return resp.text

            except httpx.HTTPStatusError as exc:
                log.warning("avito.fetch.http_error", status=exc.response.status_code,
                            attempt=attempt, url=url)
                await asyncio.sleep(3 * attempt)
            except Exception as exc:
                log.warning("avito.fetch.error", error=str(exc), attempt=attempt, url=url)
                await asyncio.sleep(3 * attempt)

        log.warning("avito.fetch.gave_up", url=url)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_html(html_text: str, keyword: str) -> list[RawCompany]:
    """Parse listing cards from Avito HTML.

    Uses html.parser — lxml incorrectly strips data-* attributes on this page.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.find_all(attrs={"data-item-id": True})

    companies: list[RawCompany] = []
    for card in cards:
        company = _parse_card(card, keyword)
        if company:
            companies.append(company)
    return companies


def _parse_card(card, keyword: str) -> RawCompany | None:
    item_id = card.get("data-item-id")

    # Title + link
    title_el = card.find(attrs={"data-marker": "item-title"})
    name_raw = title_el.get_text(strip=True) if title_el else None
    if not name_raw:
        return None

    href = title_el.get("href", "") if title_el else ""
    # Strip ?context=... tracking parameter from the URL
    clean_path = href.split("?")[0]
    source_link = f"{AVITO_BASE}{clean_path}" if clean_path.startswith("/") else None

    # Location / address
    loc_el = card.find(attrs={"data-marker": "item-location"})
    addresses: list[str] = []
    if loc_el:
        loc_text = loc_el.get_text(strip=True)
        if loc_text:
            addresses.append(loc_text)

    # Rating (try specific first, then generic)
    rating_el = card.find(attrs={"data-marker": "seller-rating/score"}) or \
                card.find(attrs={"data-marker": "seller-rating"})
    average_rating = parse_float(rating_el.get_text(strip=True)) if rating_el else None

    # Reviews count ("29 отзывов" → 29)
    reviews_el = card.find(attrs={"data-marker": "seller-info/summary"})
    reviews_count = parse_int(reviews_el.get_text(strip=True)) if reviews_el else None

    return RawCompany(
        source="avito",
        source_id=item_id,
        source_link=source_link,
        name_raw=name_raw,
        addresses=addresses,
        average_rating=average_rating,
        reviews_count=reviews_count,
        raw_payload={"keyword": keyword, "item_id": item_id},
    )


def _is_blocked(html_text: str) -> bool:
    lower = html_text[:3000].lower()
    return any(m in lower for m in _BLOCK_MARKERS)
