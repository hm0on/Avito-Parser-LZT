"""kad.arbitr.ru scraper — арбитражные дела."""

import re

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

ARBITR_SEARCH_URL = "https://kad.arbitr.ru/Kad/SearchInstances"


class ArbitrChecker(AbstractChecker):
    registry_name = "kad_arbitr"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        if not inn and not ogrn:
            return CheckResult(registry=self.registry_name, found=False, error="INN or OGRN required")

        payload = {
            "Sides": [{"Inn": inn or "", "Ogrn": ogrn or ""}],
            "Page": 1,
            "Count": 10,
            "DateFrom": None,
            "DateTo": None,
            "SessionId": "",
            "CaseType": None,
        }

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://kad.arbitr.ru/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(ARBITR_SEARCH_URL, json=payload, headers=headers)
            if resp.status_code in (403, 429):
                return CheckResult(
                    registry=self.registry_name,
                    found=False,
                    error=f"rate limited: {resp.status_code}",
                )
            resp.raise_for_status()
            data = resp.json()

        result = data.get("Result", {})
        items = result.get("Items", [])
        total_count = result.get("TotalCount", 0)

        if not items:
            return CheckResult(registry=self.registry_name, found=False)

        log.info("arbitr.check.done", inn=inn, total_count=total_count)

        # Determine if any case is active/ongoing
        active_cases = [
            i for i in items if i.get("StateId") in ("a", "r")  # active/resumed
        ]

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="has_cases",
            details={
                "total_count": total_count,
                "active_cases_count": len(active_cases),
                "sample_cases": [
                    {
                        "case_id": i.get("CaseId"),
                        "case_date": i.get("Date"),
                        "plaintiffs": [s.get("Name") for s in i.get("Sides", []) if s.get("SideTypeId") == "1"],
                        "defendants": [s.get("Name") for s in i.get("Sides", []) if s.get("SideTypeId") == "2"],
                        "state": i.get("StateName"),
                    }
                    for i in items[:5]
                ],
            },
        )
