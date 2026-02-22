"""Реестр дисквалифицированных лиц (service.nalog.ru/disqualified.do).

Pass 2 checker — requires director FIO from DaData/FNS PB results.
"""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_SEARCH_URL = "https://service.nalog.ru/disqualified-search.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://service.nalog.ru/disqualified.do",
}


def _split_fio(fio: str) -> tuple[str, str, str]:
    """Split 'Фамилия Имя Отчество' into (fam, nam, otch)."""
    parts = fio.strip().split()
    fam = parts[0] if len(parts) >= 1 else ""
    nam = parts[1] if len(parts) >= 2 else ""
    otch = parts[2] if len(parts) >= 3 else ""
    return fam, nam, otch


class FnsDisqualifiedChecker(AbstractChecker):
    registry_name = "fns_disqualified"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        director_fio = (extra or {}).get("director_fio", "")
        if not director_fio:
            return CheckResult(registry=self.registry_name, found=False, error="director FIO not available")

        fam, nam, otch = _split_fio(director_fio)
        if not fam:
            return CheckResult(registry=self.registry_name, found=False, error="could not parse director FIO")

        form_data = {
            "fam": fam,
            "nam": nam,
            "otch": otch,
            "pageSize": "10",
            "page": "1",
        }

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS, follow_redirects=True) as client:
            # Establish session
            await client.get("https://service.nalog.ru/disqualified.do")
            resp = await client.post(_SEARCH_URL, data=form_data)

            if resp.status_code in (403, 429):
                return CheckResult(registry=self.registry_name, found=False, error=f"HTTP {resp.status_code}")

            # Check for CAPTCHA
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                text = resp.text.lower()
                if "captcha" in text or "smartcaptcha" in text:
                    return CheckResult(registry=self.registry_name, found=False, error="captcha_required")

            resp.raise_for_status()
            data = resp.json()

        items = data.get("rows") or data.get("items") or data.get("data") or []
        if not items:
            log.info("fns_disqualified.clean", fio=director_fio)
            return CheckResult(
                registry=self.registry_name,
                found=True,
                status="clean",
                details={"disqualified": False, "checked_fio": director_fio, "records": []},
            )

        records: list[dict] = []
        for item in items[:5]:
            records.append({
                "fio": item.get("fio") or f"{fam} {nam} {otch}".strip(),
                "position": item.get("position") or item.get("dolzhnost") or "",
                "organization": item.get("organization") or item.get("orgName") or "",
                "period_from": item.get("periodFrom") or item.get("dateFrom") or "",
                "period_to": item.get("periodTo") or item.get("dateTo") or "",
                "basis": item.get("basis") or item.get("osnov") or "",
            })

        log.warning("fns_disqualified.found", fio=director_fio, count=len(records))

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="disqualified",
            details={
                "disqualified": True,
                "checked_fio": director_fio,
                "records": records,
            },
        )
