"""VK web scraper — fetches wall posts from public group pages via Playwright.

Works without a VK API token by scraping the mobile version of VK (m.vk.com),
which renders public group content without requiring authentication.
"""

from __future__ import annotations

import re

import structlog
from playwright.async_api import async_playwright

from src.collectors.base import RawReview
from src.enrichment.review_parsers.base import AbstractReviewParser
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

# Skip URLs that point to a single wall post rather than a group page
_WALL_POST_RE = re.compile(r"/wall-?\d+_\d+")

# Mobile VK post text selectors (in order of preference)
_POST_SELECTORS = [
    ".pi_text",          # Standard mobile post body
    "._post_content",    # Alternative layout
    ".wall_post_text",   # Yet another variant
]


class VkParser(AbstractReviewParser):
    source_name = "vk"
    supported_domains = ["vk.com", "m.vk.com"]

    async def parse(self, url: str, company_name: str) -> list[RawReview]:
        if _WALL_POST_RE.search(url):
            return []

        screen_name = self._extract_screen_name(url)
        if not screen_name:
            return []

        log.info("vk_parser.start", screen_name=screen_name)
        try:
            reviews = await self._scrape(screen_name)
            log.info("vk_parser.done", screen_name=screen_name, count=len(reviews))
            return reviews
        except Exception as exc:
            log.warning("vk_parser.error", screen_name=screen_name, error=str(exc))
            return []

    # ------------------------------------------------------------------ helpers

    def _extract_screen_name(self, url: str) -> str | None:
        m = re.search(r"vk\.com/([^/?#]+)", url)
        if m:
            name = m.group(1)
            # Numeric-only paths are user profiles, not groups
            return name if not name.isdigit() else None
        return None

    async def _scrape(self, screen_name: str) -> list[RawReview]:
        async with async_playwright() as pw:
            proxy = proxy_manager.playwright_proxy()
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    # Use a mobile user-agent so VK serves the simpler m.vk.com layout
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/17.0 Mobile/15E148 Safari/604.1"
                    ),
                    locale="ru-RU",
                )
                page = await context.new_page()
                mobile_url = f"https://m.vk.com/{screen_name}"
                await page.goto(mobile_url, wait_until="domcontentloaded", timeout=30_000)
                return await self._extract_posts(page, screen_name)
            finally:
                await browser.close()

    async def _extract_posts(self, page, screen_name: str) -> list[RawReview]:
        reviews: list[RawReview] = []

        for selector in _POST_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                if not elements:
                    continue
                texts = []
                for el in elements:
                    text = (await el.inner_text()).strip()
                    if text and len(text) > 15:
                        texts.append(text)
                if texts:
                    for text in texts[:50]:
                        reviews.append(
                            RawReview(
                                source=self.source_name,
                                text=text,
                                rating=None,
                                author=None,
                                review_date=None,
                                source_link=f"https://vk.com/{screen_name}",
                            )
                        )
                    break  # found posts with this selector, no need to try others
            except Exception:
                continue

        return reviews
