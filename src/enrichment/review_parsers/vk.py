"""VK review parser with proxy-only Playwright access."""

from __future__ import annotations

import re

import structlog
from playwright.async_api import async_playwright

from src.collectors.base import RawReview
from src.enrichment.review_parsers.base import AbstractReviewParser
from src.proxy import enrichment_proxy_manager, iter_playwright_proxies

log = structlog.get_logger(__name__)

_WALL_POST_RE = re.compile(r"/wall-?\d+_\d+")
_POST_SELECTORS = [
    ".pi_text",
    "._post_content",
    ".wall_post_text",
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

    def _extract_screen_name(self, url: str) -> str | None:
        match = re.search(r"vk\.com/([^/?#]+)", url)
        if match:
            name = match.group(1)
            return name if not name.isdigit() else None
        return None

    async def _scrape(self, screen_name: str) -> list[RawReview]:
        async with async_playwright() as pw:
            for proxy in iter_playwright_proxies(enrichment_proxy_manager, purpose="vk parser"):
                browser = await pw.chromium.launch(
                    headless=True,
                    proxy=proxy,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    context = await browser.new_context(
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
                    reviews = await self._extract_posts(page, screen_name)
                    if reviews:
                        return reviews
                except Exception as exc:
                    log.warning("vk_parser.proxy_error", screen_name=screen_name, error=str(exc))
                finally:
                    await browser.close()
        return []

    async def _extract_posts(self, page, screen_name: str) -> list[RawReview]:
        reviews: list[RawReview] = []

        for selector in _POST_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                if not elements:
                    continue
                texts = []
                for element in elements:
                    text = (await element.inner_text()).strip()
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
                    break
            except Exception:
                continue

        return reviews
