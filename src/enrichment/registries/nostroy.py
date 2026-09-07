"""НОСТРОЙ (СРО строителей) registry checker."""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import enrichment_proxy_manager, iter_proxy_urls

log = structlog.get_logger(__name__)

NOSTROY_SEARCH_URL = "https://reestr.nostroy.ru/api/sro/all/member/search"


class NostroyChecker(AbstractChecker):
    registry_name = "nostroy"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        if not inn and not name:
            return CheckResult(registry=self.registry_name, found=False, error="INN or name required")

        payload = {
            "filters": {
                "inn": inn or "",
                "fullName": name or "",
            },
            "page": 1,
            "pageCount": 10,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://reestr.nostroy.ru/",
        }

        data = {}
        last_error = ""
        for proxy_url in iter_proxy_urls(enrichment_proxy_manager, purpose="nostroy registry"):
            try:
                async with httpx.AsyncClient(timeout=20.0, proxy=proxy_url) as client:
                    resp = await client.post(NOSTROY_SEARCH_URL, json=payload, headers=headers)
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

        items = data.get("data", []) or data.get("items", [])
        if not items:
            return CheckResult(registry=self.registry_name, found=False)

        log.info("nostroy.check.done", inn=inn, count=len(items))

        member = items[0]
        is_active = member.get("memberStatus", "").lower() in ("active", "действует", "действующий")

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="active" if is_active else "inactive",
            details={
                "member_name": member.get("fullName") or member.get("shortName"),
                "inn": member.get("inn"),
                "ogrn": member.get("ogrn"),
                "sro_name": member.get("sroShortName") or member.get("sroFullName"),
                "reg_number": member.get("registrationNumber"),
                "admission_date": member.get("admissionDate"),
                "status": member.get("memberStatus"),
                "is_active": is_active,
            },
        )
