"""2GIS Catalog API collector."""

import asyncio
from urllib.parse import quote_plus

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import AbstractCollector, RawCompany, RawReview
from src.config import settings

log = structlog.get_logger(__name__)

CATALOG_URL = "https://catalog.api.2gis.com/3.0/items"
REVIEWS_URL = "https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"


class TwoGisCollector(AbstractCollector):
    source_name = "2gis"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def collect(self, keyword: str) -> list[RawCompany]:
        if not settings.twogis_api_key:
            log.warning("twogis.collect.skip", reason="TWOGIS_API_KEY not set")
            return []

        log.info("twogis.collect.start", keyword=keyword)
        companies = await self._fetch_items(keyword)
        log.info("twogis.collect.done", keyword=keyword, count=len(companies))
        return companies

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _fetch_items(self, keyword: str) -> list[RawCompany]:
        client = await self._get_client()
        params = {
            "q": keyword,
            "region_id": settings.twogis_region_id,
            "key": settings.twogis_api_key,
            "fields": "items.point,items.address,items.contact_groups,items.rubrics,items.reviews",
            "page_size": 50,
            "type": "branch",
        }

        all_companies: list[RawCompany] = []
        page = 1

        while True:
            params["page"] = page
            resp = await client.get(CATALOG_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            items = data.get("result", {}).get("items", [])
            if not items:
                break

            for item in items:
                company = self._parse_item(item, keyword)
                if company:
                    # Fetch reviews separately
                    if item.get("id"):
                        reviews = await self._fetch_reviews(item["id"])
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

        # Phones
        phones: list[str] = []
        for cg in item.get("contact_groups", []):
            for contact in cg.get("contacts", []):
                if contact.get("type") == "phone":
                    val = contact.get("value", "")
                    if val:
                        phones.append(val)

        # Address
        addresses: list[str] = []
        addr = item.get("address", {})
        if addr:
            full_addr = addr.get("name") or addr.get("components", [{}])[0].get("street_address")
            if full_addr:
                addresses.append(full_addr)

        # Point (geo)
        point = item.get("point", {})

        # Rating
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

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def _fetch_reviews(self, branch_id: str) -> list[RawReview]:
        if not settings.twogis_api_key:
            return []
        client = await self._get_client()
        url = REVIEWS_URL.format(branch_id=branch_id)
        params = {"key": settings.twogis_api_key, "page_size": 50, "is_advertiser": "false"}

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
