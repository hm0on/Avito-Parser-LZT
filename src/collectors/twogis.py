"""2GIS scraper — captures the embedded API key via Playwright, then uses catalog API."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse, parse_qs

import httpx
import structlog
from playwright.async_api import async_playwright
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import AbstractCollector, RawCompany, RawReview
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

_CATALOG_HOST = "catalog.api.2gis.com"
_CATALOG_URL = f"https://{_CATALOG_HOST}/3.0/items"
_REVIEWS_URL = "https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"
_HOME_PAGE = "https://2gis.ru/omsk"

# Public aliases kept for backward-compat with tests
CATALOG_URL = _CATALOG_URL
REVIEWS_URL = _REVIEWS_URL


class TwoGisCollector(AbstractCollector):
    source_name = "2gis"

    # Class-level cache — key is extracted once and reused across keywords
    _api_key: str | None = None

    # ------------------------------------------------------------------ public

    async def collect(self, keyword: str) -> list[RawCompany]:
        log.info("twogis.collect.start", keyword=keyword)

        api_key = await self._ensure_api_key()
        if not api_key:
            log.warning("twogis.collect.skip", reason="could not obtain embedded API key")
            return []

        companies = await self._fetch_items(keyword, api_key)
        log.info("twogis.collect.done", keyword=keyword, count=len(companies))
        return companies

    # ----------------------------------------------------------------- key capture

    async def _ensure_api_key(self) -> str | None:
        if TwoGisCollector._api_key:
            return TwoGisCollector._api_key

        key = await self._capture_api_key()
        if key:
            TwoGisCollector._api_key = key
            log.info("twogis.api_key_captured", key=key[:8] + "***")
        return key

    async def _capture_api_key(self) -> str | None:
        """Open 2gis.ru with Playwright and intercept catalog API requests to extract the key."""
        captured: list[str] = []

        async with async_playwright() as pw:
            proxy = proxy_manager.playwright_proxy()
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
                    if _CATALOG_HOST in request.url and not captured:
                        params = parse_qs(urlparse(request.url).query)
                        if "key" in params:
                            captured.append(params["key"][0])

                page.on("request", _on_request)

                try:
                    await page.goto(_HOME_PAGE, wait_until="networkidle", timeout=45_000)
                    await asyncio.sleep(2)
                except Exception as exc:
                    log.warning("twogis.key_capture.error", error=str(exc))
            finally:
                await browser.close()

        return captured[0] if captured else None

    # ----------------------------------------------------------------- data fetching

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_items(self, keyword: str, api_key: str) -> list[RawCompany]:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
                resp = await client.get(_CATALOG_URL, params=params)

                # If our captured key has expired, clear cache so next call re-captures it
                if resp.status_code in (401, 403):
                    TwoGisCollector._api_key = None
                    log.warning("twogis.api_key_expired", status=resp.status_code)
                    return []

                resp.raise_for_status()
                data = resp.json()

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
        url = _REVIEWS_URL.format(branch_id=branch_id)
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
