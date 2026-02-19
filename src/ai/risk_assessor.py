"""Risk assessment — rule-based + optional AI classification."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

RiskLevel = str  # 'green' | 'yellow' | 'red'


class RiskAssessor:
    """Assigns risk_level and risk_reasons to a canonical company card."""

    async def assess(
        self,
        checks: dict,
        reviews: list[dict] | None = None,
        company_name: str = "",
    ) -> tuple[RiskLevel, list[str]]:
        """Return (risk_level, risk_reasons).

        Rules (applied in priority order — first match wins for red/yellow):
        - RED: bankruptcy confirmed, or FSSP debt > 1M RUB, or active court losses
        - YELLOW: FSSP has executions < 1M RUB, or disputable reviews, or low confidence
        - GREEN: no adverse findings
        """
        reasons: list[str] = []

        # ── RED conditions ──────────────────────────────────────────────
        efrsb = checks.get("efrsb", {})
        if efrsb.get("found") and efrsb.get("status") == "bankrupt":
            reasons.append(
                "Компания внесена в реестр банкротов (ЕФРСБ). "
                f"https://bankrot.fedresurs.ru"
            )

        fssp = checks.get("fssp", {})
        fssp_debt = fssp.get("details", {}).get("total_debt_rub", 0)
        if fssp.get("found") and fssp_debt >= 1_000_000:
            reasons.append(
                f"Исполнительные производства ФССП на сумму {fssp_debt:,.0f} руб. "
                "https://api.fssp.gov.ru"
            )

        arbitr = checks.get("kad_arbitr", {})
        if arbitr.get("found") and arbitr.get("details", {}).get("active_cases_count", 0) >= 3:
            n = arbitr["details"]["active_cases_count"]
            reasons.append(
                f"Активных арбитражных дел: {n}. "
                "https://kad.arbitr.ru"
            )

        dadata = checks.get("dadata_fns", {})
        if dadata.get("found") and dadata.get("status", "").lower() in ("liquidated", "bankrupt"):
            reasons.append(
                f"Компания ликвидирована или признана банкротом по данным ФНС "
                "(статус: {}).".format(dadata.get("status"))
            )

        if reasons:
            return "red", reasons[:5]

        # ── YELLOW conditions ────────────────────────────────────────────
        if fssp.get("found") and 0 < fssp_debt < 1_000_000:
            reasons.append(
                f"Исполнительные производства ФССП: {fssp_debt:,.0f} руб."
            )

        if arbitr.get("found") and arbitr.get("details", {}).get("total_count", 0) > 0:
            n = arbitr["details"]["total_count"]
            reasons.append(f"В арбитражном суде найдено дел: {n}. https://kad.arbitr.ru")

        # Negative review ratio
        if reviews:
            negative = sum(1 for r in reviews if (r.get("rating") or 5) <= 2)
            ratio = negative / len(reviews)
            if ratio >= 0.3:
                reasons.append(
                    f"Высокая доля негативных отзывов: {ratio:.0%} ({negative} из {len(reviews)})."
                )

        # Not in SRO registry for строительные работы
        nostroy = checks.get("nostroy", {})
        if not nostroy.get("found"):
            reasons.append("Компания не найдена в реестре НОСТРОЙ (СРО строителей).")

        if reasons:
            return "yellow", reasons[:5]

        return "green", []
