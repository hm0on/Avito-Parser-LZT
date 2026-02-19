"""DaData API checker — ФНС/ЕГРЮЛ/ЕГРИП lookup."""

import httpx
import structlog

from src.config import settings
from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


class DaDataChecker(AbstractChecker):
    registry_name = "dadata_fns"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        query = inn or ogrn
        if not query:
            return CheckResult(registry=self.registry_name, found=False, error="no INN/OGRN provided")

        if not settings.dadata_api_key:
            return CheckResult(registry=self.registry_name, found=False, error="DADATA_API_KEY not set")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Token {settings.dadata_api_key}",
            "X-Secret": settings.dadata_secret_key,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                DADATA_SUGGEST_URL,
                json={"query": query, "count": 1},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        suggestions = data.get("suggestions", [])
        if not suggestions:
            return CheckResult(registry=self.registry_name, found=False)

        suggestion = suggestions[0]
        party = suggestion.get("data", {})
        status = party.get("state", {}).get("status", "").lower()  # ACTIVE / LIQUIDATED / etc.
        name_full = suggestion.get("value", "")
        ogrn_result = party.get("ogrn")
        inn_result = party.get("inn")
        opf = party.get("opf", {})
        entity_type = _detect_entity_type(opf)

        log.info("dadata.check.done", inn=inn, status=status, name=name_full)

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status=status,
            details={
                "name": name_full,
                "inn": inn_result,
                "ogrn": ogrn_result,
                "entity_type": entity_type,
                "registration_date": party.get("state", {}).get("registration_date"),
                "liquidation_date": party.get("state", {}).get("liquidation_date"),
                "address": party.get("address", {}).get("value"),
                "opf_short": opf.get("short"),
                "okved": party.get("okved"),
            },
        )


def _detect_entity_type(opf: dict) -> str:
    code = str(opf.get("code", ""))
    short = opf.get("short", "").upper()
    if "ИП" in short or code.startswith("5"):
        return "ИП"
    if short in ("ООО", "АО", "ПАО", "ЗАО", "ОАО"):
        return "ЮЛ"
    if short:
        return "ЮЛ"
    return "unknown"
