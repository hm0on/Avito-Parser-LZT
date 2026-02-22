"""ЕИС (zakupki.gov.ru) open data API checker."""

import httpx
import structlog

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

EIS_CONTRACTS_URL = "https://zakupki.gov.ru/epz/contract/search/results.html"
EIS_API_BASE = "https://zakupki.gov.ru/epz/contract/ws/types"

# ЕИС opendata endpoint (free, no auth required)
OPENDATA_PARTICIPANTS = "https://opendata.zakupki.gov.ru/api/export/ExportService?method=GetParticipantsByINN"


class EisChecker(AbstractChecker):
    registry_name = "eis_zakupki"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        if not inn:
            return CheckResult(registry=self.registry_name, found=False, error="INN required for EIS check")

        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        # Use the EIS ODS (open data service) for contract participant info
        params = {"inn": inn}
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(
                    OPENDATA_PARTICIPANTS,
                    params=params,
                    headers=headers,
                )
                if resp.status_code in (403, 404, 429):
                    return CheckResult(
                        registry=self.registry_name,
                        found=False,
                        error=f"EIS API returned {resp.status_code}",
                    )
                data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
            except Exception as exc:
                return CheckResult(registry=self.registry_name, found=False, error=str(exc))

        participants = data.get("participants") or []
        if not participants:
            return CheckResult(registry=self.registry_name, found=False)

        participant = participants[0] if participants else {}
        log.info("eis.check.done", inn=inn)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="participant",
            details={
                "name": participant.get("name"),
                "inn": participant.get("inn"),
                "contracts_count": participant.get("contractsCount"),
                "contracts_sum": participant.get("contractsSum"),
                "rmp_included": participant.get("isRmpIncluded", False),
            },
        )
