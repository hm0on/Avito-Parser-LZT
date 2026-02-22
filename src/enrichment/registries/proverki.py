"""Реестр проверок (proverki.gov.ru) — история надзорных мероприятий."""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_SEARCH_URL = "https://proverki.gov.ru/portal/public-search/api/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://proverki.gov.ru/portal/public-search",
}


class ProverkiChecker(AbstractChecker):
    registry_name = "proverki"

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

        payload = {
            "inn": inn or "",
            "ogrn": ogrn or "",
            "page": 0,
            "size": 10,
            "sort": "dateStart,desc",
        }

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            resp = await client.post(_SEARCH_URL, json=payload)

            if resp.status_code in (403, 429):
                return CheckResult(registry=self.registry_name, found=False, error=f"HTTP {resp.status_code}")

            resp.raise_for_status()
            data = resp.json()

        # Response may have various structures
        items = data.get("content") or data.get("items") or data.get("data") or []
        total = data.get("totalElements") or data.get("total") or len(items)

        if not items and total == 0:
            return CheckResult(registry=self.registry_name, found=False)

        with_violations = 0
        sample: list[dict] = []

        for item in items[:10]:
            has_violation = bool(
                item.get("hasViolations")
                or item.get("violationsFound")
                or (item.get("result") and "нарушен" in str(item.get("result", "")).lower())
            )
            if has_violation:
                with_violations += 1

            if len(sample) < 5:
                sample.append({
                    "authority": item.get("authorityName") or item.get("controlBody") or "",
                    "start_date": item.get("dateStart") or item.get("startDate") or "",
                    "end_date": item.get("dateEnd") or item.get("endDate") or "",
                    "type": item.get("checkType") or item.get("type") or "",
                    "result": item.get("result") or item.get("resultText") or "",
                    "has_violations": has_violation,
                })

        log.info("proverki.done", total=total, with_violations=with_violations)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="has_violations" if with_violations > 0 else "clean",
            details={
                "inspections_count": total,
                "with_violations": with_violations,
                "inspections_sample": sample,
            },
        )
