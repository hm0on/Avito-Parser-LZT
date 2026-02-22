"""Проверка адреса на массовость (service.nalog.ru/addrfind.do).

Pass 2 checker — requires legal address from DaData/FNS PB results.
"""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_PROC_URL = "https://service.nalog.ru/addrfind-proc.json"
_RESULT_URL = "https://service.nalog.ru/addrfind-search.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://service.nalog.ru/addrfind.do",
}


class FnsMassAddressChecker(AbstractChecker):
    registry_name = "fns_mass_address"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        address = (extra or {}).get("legal_address", "")
        if not address:
            return CheckResult(registry=self.registry_name, found=False, error="legal address not available")

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS, follow_redirects=True) as client:
            # Establish session
            await client.get("https://service.nalog.ru/addrfind.do")

            # Step 1: start search
            resp = await client.post(_PROC_URL, data={"query": address})

            if resp.status_code in (403, 429):
                return CheckResult(registry=self.registry_name, found=False, error=f"HTTP {resp.status_code}")

            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                text = resp.text.lower()
                if "captcha" in text or "smartcaptcha" in text:
                    return CheckResult(registry=self.registry_name, found=False, error="captcha_required")

            resp.raise_for_status()
            data = resp.json()

            token = data.get("t")
            if not token:
                # Some versions return results directly
                return self._parse_result(data, address)

            # Step 2: poll results
            resp2 = await client.post(_RESULT_URL, data={"token": token})
            resp2.raise_for_status()
            result = resp2.json()

        return self._parse_result(result, address)

    def _parse_result(self, data: dict, address: str) -> CheckResult:
        items = data.get("rows") or data.get("items") or data.get("addresses") or []

        if not items:
            log.info("fns_mass_address.clean", address=address[:50])
            return CheckResult(
                registry=self.registry_name,
                found=True,
                status="clean",
                details={
                    "is_mass_address": False,
                    "companies_count": 0,
                    "address_checked": address,
                },
            )

        # If results found, the address is mass-registration
        companies_count = 0
        for item in items:
            cnt = item.get("cnt") or item.get("count") or item.get("companiesCount") or 0
            try:
                companies_count = max(companies_count, int(cnt))
            except (ValueError, TypeError):
                pass

        # Consider mass if 10+ companies at this address
        is_mass = companies_count >= 10 or len(items) > 0

        log.info("fns_mass_address.done", address=address[:50], companies=companies_count, is_mass=is_mass)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="mass_address" if is_mass else "clean",
            details={
                "is_mass_address": is_mass,
                "companies_count": companies_count,
                "address_checked": address,
            },
        )
