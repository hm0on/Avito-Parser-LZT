"""Unit tests for the rule-based risk assessor."""

import pytest

from src.ai.risk_assessor import RiskAssessor


@pytest.fixture
def assessor() -> RiskAssessor:
    return RiskAssessor()


# ── RED conditions ──────────────────────────────────────────────────────────


async def test_red_bankrupt_efrsb(assessor):
    checks = {"efrsb": {"found": True, "status": "bankrupt"}}
    level, reasons = await assessor.assess(checks)
    assert level == "red"
    assert any("банкрот" in r.lower() for r in reasons)


async def test_red_fssp_large_debt(assessor):
    checks = {"fssp": {"found": True, "details": {"total_debt_rub": 2_500_000}}}
    level, reasons = await assessor.assess(checks)
    assert level == "red"
    assert any("ФССП" in r for r in reasons)


async def test_red_many_active_arbitr_cases(assessor):
    checks = {"kad_arbitr": {"found": True, "details": {"active_cases_count": 5, "total_count": 10}}}
    level, reasons = await assessor.assess(checks)
    assert level == "red"
    assert any("арбитраж" in r.lower() for r in reasons)


async def test_red_dadata_liquidated(assessor):
    checks = {"dadata_fns": {"found": True, "status": "liquidated"}}
    level, reasons = await assessor.assess(checks)
    assert level == "red"
    assert any("ликвидирован" in r.lower() for r in reasons)


# ── YELLOW conditions ───────────────────────────────────────────────────────


async def test_yellow_small_fssp_debt(assessor):
    checks = {"fssp": {"found": True, "details": {"total_debt_rub": 50_000}}}
    level, reasons = await assessor.assess(checks)
    assert level == "yellow"
    assert any("ФССП" in r for r in reasons)


async def test_yellow_high_negative_reviews(assessor):
    reviews = [
        {"rating": 1}, {"rating": 1}, {"rating": 1},
        {"rating": 5}, {"rating": 5},
    ]
    checks = {"nostroy": {"found": True}}
    level, reasons = await assessor.assess(checks, reviews=reviews)
    assert level == "yellow"
    assert any("негативных" in r for r in reasons)


async def test_yellow_not_in_nostroy(assessor):
    """No adverse checks, but company not in NOSTROY → yellow."""
    checks = {}
    level, reasons = await assessor.assess(checks)
    assert level == "yellow"
    assert any("НОСТРОЙ" in r for r in reasons)


async def test_yellow_arbitr_few_cases(assessor):
    checks = {
        "kad_arbitr": {"found": True, "details": {"active_cases_count": 1, "total_count": 3}},
        "nostroy": {"found": True},
    }
    level, reasons = await assessor.assess(checks)
    assert level == "yellow"


# ── GREEN condition ─────────────────────────────────────────────────────────


async def test_green_no_adverse_findings(assessor):
    checks = {
        "nostroy": {"found": True},
        "fssp": {"found": False},
        "efrsb": {"found": False},
        "kad_arbitr": {"found": False},
        "dadata_fns": {"found": True, "status": "active"},
    }
    level, reasons = await assessor.assess(checks)
    assert level == "green"
    assert reasons == []


async def test_green_positive_reviews_only(assessor):
    reviews = [{"rating": 5}, {"rating": 5}, {"rating": 4}]
    checks = {"nostroy": {"found": True}}
    level, reasons = await assessor.assess(checks, reviews=reviews)
    assert level == "green"


# ── Reasons count cap ──────────────────────────────────────────────────────


async def test_reasons_capped_at_five(assessor):
    """Never return more than 5 reasons."""
    checks = {
        "efrsb": {"found": True, "status": "bankrupt"},
        "fssp": {"found": True, "details": {"total_debt_rub": 3_000_000}},
        "kad_arbitr": {"found": True, "details": {"active_cases_count": 10, "total_count": 10}},
        "dadata_fns": {"found": True, "status": "liquidated"},
    }
    _, reasons = await assessor.assess(checks)
    assert len(reasons) <= 5
