"""ФССП (Federal Bailiff Service) API checker."""

import httpx
import structlog

from src.config import settings
from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

FSSP_API_URL = "https://api.fssp.gov.ru/api/v1.0/debtor"


class FsspChecker(AbstractChecker):
    registry_name = "fssp"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        if not name and not inn:
            return CheckResult(registry=self.registry_name, found=False, error="name or INN required")

        if not settings.fssp_api_token:
            return CheckResult(registry=self.registry_name, found=False, error="FSSP_API_TOKEN not set")

        params = {
            "token": settings.fssp_api_token,
            "is_ip": "true",  # юрлица тоже
        }
        if inn:
            params["inn"] = inn
        elif name:
            params["name"] = name

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(FSSP_API_URL, params=params)
            if resp.status_code == 404:
                return CheckResult(registry=self.registry_name, found=False)
            resp.raise_for_status()
            data = resp.json()

        result = data.get("result", {})
        items = result.get("items", [])

        if not items:
            return CheckResult(registry=self.registry_name, found=False)

        total_debt = sum(float(item.get("sum", 0) or 0) for item in items)
        log.info("fssp.check.done", inn=inn, executions=len(items), total_debt=total_debt)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="has_executions",
            details={
                "executions_count": len(items),
                "total_debt_rub": total_debt,
                "items_sample": items[:5],
            },
        )
