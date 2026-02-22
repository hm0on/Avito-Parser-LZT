"""ФНС «Прозрачный бизнес» (pb.nalog.ru) — risk markers, director, address."""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_SEARCH_URL = "https://pb.nalog.ru/search-proc.json"
_RESULT_URL = "https://pb.nalog.ru/company-search.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://pb.nalog.ru/",
    "Origin": "https://pb.nalog.ru",
}


class FnsPbChecker(AbstractChecker):
    registry_name = "fns_pb"

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

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS, follow_redirects=True) as client:
            # Step 1: start search → get token
            resp = await client.post(_SEARCH_URL, data={"query": query, "page": "1", "pageSize": "1"})
            resp.raise_for_status()
            data = resp.json()

            token = data.get("t")
            if not token:
                return CheckResult(registry=self.registry_name, found=False, error="no search token")

            # Step 2: poll results
            resp2 = await client.post(_RESULT_URL, data={"token": token})
            resp2.raise_for_status()
            result = resp2.json()

        items = result.get("companies", result.get("ul", result.get("items", [])))
        if not items:
            return CheckResult(registry=self.registry_name, found=False)

        company = items[0] if isinstance(items, list) else items
        if isinstance(company, dict):
            return self._parse(company)

        return CheckResult(registry=self.registry_name, found=False)

    def _parse(self, c: dict) -> CheckResult:
        # Extract risk markers from various PB fields
        risk_markers: list[str] = []

        # Common risk flag fields in PB response
        for key in ("massovodirector", "massovoadress", "disqualified", "invalid"):
            val = c.get(key)
            if val and str(val).strip() not in ("0", "", "false", "нет"):
                risk_markers.append(key)

        # Text-based warnings
        for key in ("warnings", "risk_markers", "risks"):
            val = c.get(key)
            if isinstance(val, list):
                risk_markers.extend(str(v) for v in val)
            elif isinstance(val, str) and val.strip():
                risk_markers.append(val)

        head_fio = c.get("headFio") or c.get("head", {}).get("fio", "") if isinstance(c.get("head"), dict) else c.get("headFio", "")
        address = c.get("adress") or c.get("address") or ""
        status_msg = c.get("statusMessage") or c.get("status") or ""

        log.info("fns_pb.done", inn=c.get("inn"), risks=len(risk_markers))

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="has_risks" if risk_markers else "clean",
            details={
                "name": c.get("nameFull") or c.get("name", ""),
                "head_fio": head_fio,
                "address": address,
                "status_message": status_msg,
                "risk_markers": risk_markers,
                "has_risk_markers": bool(risk_markers),
                "registration_date": c.get("regDate") or c.get("registrationDate"),
                "okved": c.get("okved"),
            },
        )
