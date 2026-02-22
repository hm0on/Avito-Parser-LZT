"""Реестр МСП (rmsp.nalog.ru) — проверка статуса малого/среднего предприятия."""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_SEARCH_URL = "https://rmsp.nalog.ru/api/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_CATEGORY_MAP = {
    "1": "micro",
    "2": "small",
    "3": "medium",
}


class FnsMspChecker(AbstractChecker):
    registry_name = "fns_msp"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        query = inn or ogrn
        if not query:
            return CheckResult(registry=self.registry_name, found=False, error="INN or OGRN required")

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            resp = await client.get(_SEARCH_URL, params={"query": query, "page": "0", "pageSize": "10"})

            if resp.status_code in (403, 429):
                return CheckResult(registry=self.registry_name, found=False, error=f"HTTP {resp.status_code}")

            resp.raise_for_status()
            data = resp.json()

        # Response structure varies: items/content/data
        items = data.get("items") or data.get("content") or data.get("data") or []
        if not items:
            log.info("fns_msp.not_found", query=query)
            return CheckResult(registry=self.registry_name, found=False)

        item = items[0]
        cat_code = str(item.get("category") or item.get("categoryCode") or "")
        category = _CATEGORY_MAP.get(cat_code, cat_code)

        log.info("fns_msp.found", query=query, category=category)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="msp",
            details={
                "in_msp": True,
                "category": category,
                "category_code": cat_code,
                "inclusion_date": item.get("inclusionDate") or item.get("regDate"),
                "name": item.get("name") or item.get("fullName", ""),
            },
        )
