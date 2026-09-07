import pytest

from src.collectors.base import RawCompany
from src.relevance import RelevanceService
from src.relevance.domain_filter import evaluate_domain
from src.relevance.geo_filter import evaluate_geo


def test_domain_filter_pass_for_plumbing_company():
    decision = evaluate_domain(
        name="СантехМонтаж Омск",
        keyword="монтаж водопровода",
        snippet="Аварийный сантехник, установка бойлеров и радиаторов",
    )
    assert decision.domain_pass is True
    assert decision.score >= 0.75


def test_domain_filter_rejects_obvious_non_plumbing():
    decision = evaluate_domain(
        name="Детский сад N 12",
        keyword="детский сад",
        snippet="муниципальное дошкольное учреждение",
    )
    assert decision.domain_pass is False
    assert "domain_deny_tokens" in decision.reason_codes


def test_geo_filter_pass_for_omsk_oblast_address():
    raw = RawCompany(
        source="2gis",
        name_raw="Сантех-Сервис",
        addresses=["Омская область, р.п. Марьяновка, ул. Ленина, 1"],
    )
    geo = evaluate_geo(raw)
    assert geo.geo_pass is True
    assert geo.score > 0


def test_geo_filter_rejects_outside_region():
    raw = RawCompany(
        source="2gis",
        name_raw="Сантех-Сервис",
        addresses=["Новосибирск, ул. Кирова, 12"],
        source_link="https://2gis.ru/novosibirsk/firm/123",
    )
    geo = evaluate_geo(raw)
    assert geo.geo_pass is False


@pytest.mark.asyncio
async def test_relevance_service_writes_positive_decision_for_clear_case():
    service = RelevanceService()
    raw = RawCompany(
        source="avito",
        name_raw="Сантехник 24/7",
        addresses=["Омск, проспект Мира, 2"],
        raw_payload={"title": "монтаж отопления и водопровода"},
    )
    decision = await service.decide(raw=raw, keyword="монтаж водопровода")
    assert decision.domain_pass is True
    assert decision.geo_pass is True
