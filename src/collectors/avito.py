"""Avito collector — Playwright + playwright-stealth scraper."""

import asyncio
import re
from urllib.parse import quote_plus

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import AbstractCollector, RawCompany, RawReview
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

AVITO_BASE = "https://www.avito.ru"
AVITO_SEARCH = f"{AVITO_BASE}/omsk/uslugi"


class AvitoCollector(AbstractCollector):
    source_name = "avito"

    async def collect(self, keyword: str) -> list[RawCompany]:
        url = f"{AVITO_SEARCH}?q={quote_plus(keyword)}"
        log.info("avito.collect.start", keyword=keyword, url=url)

        async with async_playwright() as pw:
            browser = await self._launch_browser(pw)
            try:
                context = await self._new_context(browser)
                listing_page = await context.new_page()
                companies = await self._scrape_listing(listing_page, url, keyword)
            finally:
                await browser.close()

        log.info("avito.collect.done", keyword=keyword, count=len(companies))
        return companies

    async def _launch_browser(self, pw) -> Browser:
        proxy = proxy_manager.playwright_proxy()
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
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        try:
            from playwright_stealth import stealth_async
            await stealth_async(context)
        except ImportError:
            log.warning("playwright_stealth not installed — bot detection not bypassed")
        return context

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _scrape_listing(self, page: Page, url: str, keyword: str) -> list[RawCompany]:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)

        # Scroll to load more items
        for _ in range(3):
            await page.keyboard.press("End")
            await asyncio.sleep(1)

        cards = await page.query_selector_all("[data-marker='item']")
        log.info("avito.listing.cards_found", count=len(cards), keyword=keyword)

        # Pass 1: parse all card metadata while still on the listing page.
        # Do NOT navigate away here — element handles become detached after navigation.
        companies: list[RawCompany] = []
        for card in cards[:30]:
            try:
                company = await self._parse_card_basic(card, keyword)
                if company:
                    companies.append(company)
            except Exception as exc:
                log.warning("avito.card.parse_error", error=str(exc))

        # Pass 2: enrich each company from its profile page using a separate page object
        # so that element handles from Pass 1 are never invalidated.
        profile_page = await page.context.new_page()
        try:
            for company in companies:
                if company.source_link:
                    try:
                        await self._enrich_from_profile(profile_page, company, company.source_link)
                    except Exception as exc:
                        log.warning("avito.profile.enrich_error", url=company.source_link, error=str(exc))
        finally:
            await profile_page.close()

        return companies

    async def _parse_card_basic(self, card, keyword: str) -> RawCompany | None:
        """Extract metadata from a listing card without navigating away."""
        title_el = await card.query_selector("[data-marker='item-title']")
        name_raw = (await title_el.inner_text()).strip() if title_el else None

        link_el = await card.query_selector("a[data-marker='item-title']")
        href = await link_el.get_attribute("href") if link_el else None
        source_link = f"{AVITO_BASE}{href}" if href and href.startswith("/") else href

        source_id = None
        if source_link:
            m = re.search(r"_(\d+)$", source_link)
            if m:
                source_id = m.group(1)

        rating_el = await card.query_selector("[data-marker='item-rating']")
        rating_text = await rating_el.inner_text() if rating_el else ""
        average_rating = _parse_float(rating_text)

        reviews_el = await card.query_selector("[data-marker='item-reviews-count']")
        reviews_text = await reviews_el.inner_text() if reviews_el else ""
        reviews_count = _parse_int(reviews_text)

        card_html = await card.inner_html()

        return RawCompany(
            source=self.source_name,
            source_id=source_id,
            source_link=source_link,
            name_raw=name_raw,
            average_rating=average_rating,
            reviews_count=reviews_count,
            raw_payload={"keyword": keyword, "card_html_snippet": card_html[:2000]},
        ) if name_raw else None

    async def _enrich_from_profile(self, page: Page, company: RawCompany, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(1)

        # Phone — Avito hides it behind a button
        phone_btn = await page.query_selector("[data-marker='phone-button']")
        if phone_btn:
            await phone_btn.click()
            await asyncio.sleep(1)
            phone_el = await page.query_selector("[data-marker='phone-number']")
            if phone_el:
                phone_text = (await phone_el.inner_text()).strip()
                if phone_text:
                    company.phones = [phone_text]

        # Email (rarely shown)
        email_el = await page.query_selector("a[href^='mailto:']")
        if email_el:
            email = await email_el.get_attribute("href")
            if email:
                company.emails = [email.replace("mailto:", "")]

        # Address
        address_el = await page.query_selector("[data-marker='item-address']")
        if address_el:
            addr_text = (await address_el.inner_text()).strip()
            if addr_text:
                company.addresses = [addr_text]

        company.reviews = await self._scrape_reviews(page, url)

    async def _scrape_reviews(self, page: Page, base_url: str) -> list[RawReview]:
        reviews: list[RawReview] = []
        review_els = await page.query_selector_all("[data-marker='review-item']")
        for el in review_els[:20]:
            text_el = await el.query_selector("[data-marker='review-text']")
            rating_el = await el.query_selector("[data-marker='review-rating']")
            author_el = await el.query_selector("[data-marker='review-author']")

            reviews.append(
                RawReview(
                    source=self.source_name,
                    text=(await text_el.inner_text()).strip() if text_el else None,
                    rating=_parse_float(
                        (await rating_el.inner_text()).strip() if rating_el else ""
                    ),
                    author=(await author_el.inner_text()).strip() if author_el else None,
                    source_link=base_url,
                )
            )
        return reviews


def _parse_float(text: str) -> float | None:
    m = re.search(r"[\d]+[.,]?[\d]*", text.replace(",", "."))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    try:
        return int(m.group()) if m else None
    except ValueError:
        return None
