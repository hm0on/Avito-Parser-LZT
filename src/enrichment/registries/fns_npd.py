"""ФНС НПД (npd.nalog.ru) — проверка статуса самозанятого по ИНН."""

from datetime import date

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_CHECK_URL = "https://npd.nalog.ru/api/v1/tracker/serviceInfo"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class FnsNpdChecker(AbstractChecker):
    registry_name = "fns_npd"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        if not inn:
            return CheckResult(registry=self.registry_name, found=False, error="INN required")

        today = date.today().isoformat()

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            resp = await client.post(
                _CHECK_URL,
                json={"inn": inn, "requestDate": today},
            )

            if resp.status_code in (403, 429):
                return CheckResult(registry=self.registry_name, found=False, error=f"HTTP {resp.status_code}")

            resp.raise_for_status()
            data = resp.json()

        # API response: {"status": true/false, "message": "..."}
        is_npd = bool(data.get("status"))
        message = data.get("message") or ""

        log.info("fns_npd.done", inn=inn, is_npd=is_npd)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="npd_active" if is_npd else "not_npd",
            details={
                "is_npd": is_npd,
                "status_message": message,
                "check_date": today,
            },
        )
