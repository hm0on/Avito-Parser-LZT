"""ЕФРСБ (Bankrupt registry) checker — fedresurs.ru scraping."""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import enrichment_proxy_manager, iter_proxy_urls

log = structlog.get_logger(__name__)

EFRSB_SEARCH_URL = "https://bankrot.fedresurs.ru/api/businesses"


class EfrsbChecker(AbstractChecker):
    registry_name = "efrsb"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        query = inn or ogrn or name
        if not query:
            return CheckResult(registry=self.registry_name, found=False, error="no search term provided")

        params = {
            "searchString": query,
            "pageSize": 10,
            "pageNum": 0,
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        data = {}
        last_error = ""
        for proxy_url in iter_proxy_urls(enrichment_proxy_manager, purpose="efrsb registry"):
            try:
                async with httpx.AsyncClient(timeout=20.0, proxy=proxy_url) as client:
                    resp = await client.get(EFRSB_SEARCH_URL, params=params, headers=headers)
                    if resp.status_code in (403, 429):
                        last_error = f"rate limited: {resp.status_code}"
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    break
            except Exception as exc:
                last_error = str(exc)
        if not data and last_error:
            return CheckResult(registry=self.registry_name, found=False, error=last_error)

        items = data.get("items") or data.get("data") or []
        if not items:
            return CheckResult(registry=self.registry_name, found=False)

        log.info("efrsb.check.done", query=query, count=len(items))

        # Check for active bankruptcy proceedings
        active = [
            i for i in items
            if "банкрот" in str(i.get("status", "")).lower()
            or i.get("isBankrupt", False)
        ]

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="bankrupt" if active else "mentioned",
            details={
                "total_found": len(items),
                "active_bankruptcies": len(active),
                "sample": [
                    {
                        "name": i.get("name") or i.get("fullName"),
                        "inn": i.get("inn"),
                        "status": i.get("status"),
                        "case_id": i.get("caseId") or i.get("arbitrCaseId"),
                    }
                    for i in items[:3]
                ],
            },
        )
