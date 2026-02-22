"""Yandex Maps collector — Playwright scraper with proxy retry."""

import asyncio
import re
from urllib.parse import quote_plus

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

YANDEX_MAPS_BASE = f"https://yandex.ru/maps/{settings.yandex_region_code}/"


class YandexCollector(AbstractCollector):
    source_name = "yandex"

    async def collect(self, keyword: str) -> list[RawCompany]:
        url = f"{YANDEX_MAPS_BASE}?text={quote_plus(keyword)}"
        log.info("yandex.collect.start", keyword=keyword, url=url)

        companies: list[RawCompany] = []

        # Try with proxy first.
        if proxy_manager.count > 0:
            try:
                companies = await self._collect_once(url, keyword, use_proxy=True)
            except Exception as exc:
                log.warning("yandex.collect.with_proxy_error", error=_exc_detail(exc))

        # Retry without proxy if no results.
        if not companies:
            log.info("yandex.collect.retry_without_proxy")
            try:
                companies = await self._collect_once(url, keyword, use_proxy=False)
            except Exception as exc:
                log.warning("yandex.collect.without_proxy_error", error=_exc_detail(exc))
                companies = []

        log.info("yandex.collect.done", keyword=keyword, count=len(companies))
        return companies

    async def _collect_once(self, url: str, keyword: str, *, use_proxy: bool) -> list[RawCompany]:
        async with async_playwright() as pw:
            browser = await self._launch_browser(pw, use_proxy=use_proxy)
            try:
                context = await self._new_context(browser)
                page = await context.new_page()
                return await self._scrape_listing(page, url, keyword)
            finally:
                await browser.close()

    async def _launch_browser(self, pw, *, use_proxy: bool) -> Browser:
        proxy = proxy_manager.playwright_proxy() if use_proxy else None
        return await pw.chromium.launch(
            headless=True,
            proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    async def _new_context(self, browser: Browser) -> BrowserContext:
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="ru-RU",
        )
        try:
            from playwright_stealth import stealth_async

            await stealth_async(context)
        except ImportError:
            pass
        return context

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _scrape_listing(self, page: Page, url: str, keyword: str) -> list[RawCompany]:
        await page.goto(url, wait_until="networkidle", timeout=45_000)
        await asyncio.sleep(3)

        # Scroll sidebar to load more results
        sidebar = await page.query_selector(".sidebar-view__panel")
        if sidebar:
            for _ in range(5):
                await sidebar.evaluate("el => el.scrollBy(0, 400)")
                await asyncio.sleep(0.8)

        # Collect listing cards (Yandex frequently renames CSS classes)
        cards = await self._find_cards(page)
        log.info("yandex.listing.cards_found", count=len(cards), keyword=keyword)

        companies: list[RawCompany] = []
        for card in cards[:25]:
            try:
                company = await self._parse_card(page, card, keyword)
                if company:
                    companies.append(company)
            except Exception as exc:
                log.warning("yandex.card.parse_error", error=str(exc))

        return companies

    async def _find_cards(self, page: Page):
        """Try multiple CSS selectors — Yandex often renames class prefixes."""
        selectors = [
            "[class*='search-snippet']",
            "[class*='search-business-snippet-view']",
            "[class*='search-business-snippet']",
            "[class*='search-snippet-view']",
        ]
        for selector in selectors:
            cards = await page.query_selector_all(selector)
            if cards:
                return cards

        # Extra wait for slow rendering
        await asyncio.sleep(2)
        for selector in selectors:
            cards = await page.query_selector_all(selector)
            if cards:
                return cards
        return []

    async def _parse_card(self, page: Page, card, keyword: str) -> RawCompany | None:
        name_el = await card.query_selector("[class*='orgcard-header__name']")
        if not name_el:
            name_el = await card.query_selector("h2")
        name_raw = (await name_el.inner_text()).strip() if name_el else None

        # Rating
        rating_el = await card.query_selector("[class*='business-rating-badge-view__rating']")
        average_rating = parse_float(
            (await rating_el.inner_text()).strip() if rating_el else ""
        )

        # Reviews count
        reviews_el = await card.query_selector("[class*='business-rating-badge-view__count']")
        reviews_count = parse_int(
            (await reviews_el.inner_text()).strip() if reviews_el else ""
        )

        # Address
        addr_el = await card.query_selector("[class*='orgcard-subtitle']")
        addresses = []
        if addr_el:
            addr_text = (await addr_el.inner_text()).strip()
            if addr_text:
                addresses = [addr_text]

        # Phone
        phone_el = await card.query_selector("[class*='contact-item_type_phone']")
        phones = []
        if phone_el:
            phone_text = (await phone_el.inner_text()).strip()
            if phone_text:
                phones = [phone_text]

        # Source link
        link_el = await card.query_selector("a[href*='maps']")
        source_link = await link_el.get_attribute("href") if link_el else None
        if source_link and not source_link.startswith("http"):
            source_link = f"https://yandex.ru{source_link}"

        source_id = None
        if source_link:
            m = re.search(r"/org/[^/]+/(\d+)", source_link)
            if m:
                source_id = m.group(1)

        card_html = await card.inner_html()

        return RawCompany(
            source=self.source_name,
            source_id=source_id,
            source_link=source_link,
            name_raw=name_raw,
            phones=phones,
            addresses=addresses,
            average_rating=average_rating,
            reviews_count=reviews_count,
            raw_payload={"keyword": keyword, "card_html_snippet": card_html[:2000]},
        ) if name_raw else None


def _exc_detail(exc: Exception) -> str:
    if isinstance(exc, RetryError):
        try:
            inner = exc.last_attempt.exception()
            if inner:
                return f"RetryError -> {type(inner).__name__}: {inner}"
        except Exception:
            pass
    return str(exc)
