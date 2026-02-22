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
        - RED: bankruptcy, FSSP debt > 1M, active court losses, RNP, disqualification
        - YELLOW: minor FSSP, court cases, bad reviews, mass address, inspections
        - GREEN: no adverse findings
        """
        reasons: list[str] = []

        # ── RED conditions ──────────────────────────────────────────────
        efrsb = checks.get("efrsb", {})
        if efrsb.get("found") and efrsb.get("status") == "bankrupt":
            reasons.append(
                "Компания внесена в реестр банкротов (ЕФРСБ). "
                "https://bankrot.fedresurs.ru"
            )

        dadata = checks.get("dadata_fns", {})
        if dadata.get("found") and dadata.get("status", "").lower() in ("liquidated", "bankrupt"):
            reasons.append(
                "Компания ликвидирована или признана банкротом по данным ФНС "
                "(статус: {}).".format(dadata.get("status"))
            )

        fssp = checks.get("fssp", {})
        fssp_debt = fssp.get("details", {}).get("total_debt_rub", 0) or 0
        if fssp.get("found") and fssp_debt >= 1_000_000:
            reasons.append(
                f"Исполнительные производства ФССП на сумму {fssp_debt:,.0f} руб. "
                "https://fssp.gov.ru/iss/ip"
            )

        arbitr = checks.get("kad_arbitr", {})
        if arbitr.get("found") and arbitr.get("details", {}).get("active_cases_count", 0) >= 3:
            n = arbitr["details"]["active_cases_count"]
            reasons.append(
                f"Активных арбитражных дел: {n}. "
                "https://kad.arbitr.ru"
            )

        # РНП — критический красный флаг
        rnp = checks.get("rnp", {})
        if rnp.get("found") and rnp.get("details", {}).get("in_rnp"):
            reasons.append(
                "Компания включена в реестр недобросовестных поставщиков (РНП). "
                "https://zakupki.gov.ru/epz/dishonestsupplier"
            )

        # Дисквалификация директора
        disq = checks.get("fns_disqualified", {})
        if disq.get("found") and disq.get("details", {}).get("disqualified"):
            fio = disq.get("details", {}).get("checked_fio", "")
            reasons.append(
                f"Руководитель дисквалифицирован ({fio}). "
                "https://service.nalog.ru/disqualified.do"
            )

        # ФНС ПБ — критические маркеры риска
        pb = checks.get("fns_pb", {})
        if pb.get("found"):
            markers = pb.get("details", {}).get("risk_markers", [])
            critical = [m for m in markers if m in ("massovodirector", "disqualified", "invalid")]
            if critical:
                reasons.append(
                    f"ФНС «Прозрачный бизнес»: критические маркеры риска ({', '.join(critical)}). "
                    "https://pb.nalog.ru"
                )

        if reasons:
            return "red", reasons[:7]

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

        # Массовый адрес
        mass = checks.get("fns_mass_address", {})
        if mass.get("found") and mass.get("details", {}).get("is_mass_address"):
            cnt = mass.get("details", {}).get("companies_count", 0)
            reasons.append(
                f"Юридический адрес — адрес массовой регистрации ({cnt} компаний). "
                "https://service.nalog.ru/addrfind.do"
            )

        # Проверки с нарушениями
        proverki = checks.get("proverki", {})
        if proverki.get("found") and proverki.get("details", {}).get("with_violations", 0) > 0:
            n = proverki["details"]["with_violations"]
            reasons.append(
                f"Проверки госорганов с нарушениями: {n}. "
                "https://proverki.gov.ru"
            )

        # ФНС ПБ — некритические маркеры
        if pb.get("found"):
            markers = pb.get("details", {}).get("risk_markers", [])
            non_critical = [m for m in markers if m not in ("massovodirector", "disqualified", "invalid")]
            if non_critical:
                reasons.append(
                    f"ФНС «Прозрачный бизнес»: маркеры ({', '.join(non_critical[:3])})."
                )

        if reasons:
            return "yellow", reasons[:7]

        return "green", []
