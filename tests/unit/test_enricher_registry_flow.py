from __future__ import annotations

import uuid
from datetime import date
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.api.schemas import CompanyDetail
from src.database.models import CompanyRaw
from src.deduplication.deduplicator import Deduplicator
from src.enrichment.enricher import Enricher
from src.enrichment.registries.base import CheckResult
from src.enrichment.registries.dadata import DaDataChecker
from src.enrichment.registries.arbitr import ArbitrChecker
from src.enrichment.registries.efrsb import EfrsbChecker
from src.enrichment.registries.eis import EisChecker
from src.enrichment.registries.nostroy import NostroyChecker
from src.pipeline.common import canonical_to_orm


@pytest.mark.asyncio
async def test_no_ids_skips_legacy_checks_when_listorg_not_found():
    enricher = Enricher()

    with (
        patch("src.enrichment.enricher.settings") as mock_settings,
        patch("src.enrichment.enricher._listorg_checker") as mock_listorg,
        patch("src.enrichment.enricher._rusprofile_checker") as mock_rusprofile,
        patch("src.enrichment.enricher._openai_company_fallback_checker") as mock_openai_fallback,
        patch("src.enrichment.enricher.asyncio.gather", new_callable=AsyncMock) as gather_mock,
    ):
        mock_settings.listorg_enabled = True
        mock_settings.listorg_primary = True
        mock_settings.listorg_allow_name_lookup = True
        mock_settings.listorg_fallback_enabled = True
        mock_settings.rusprofile_enabled = True
        mock_settings.openai_company_fallback_enabled = True
        mock_settings.enable_fssp_check = False

        mock_listorg.safe_check = AsyncMock(
            return_value=CheckResult(registry="listorg", found=False, details={})
        )
        mock_rusprofile.safe_check = AsyncMock(
            return_value=CheckResult(registry="rusprofile", found=False, details={})
        )
        mock_openai_fallback.safe_check = AsyncMock(
            return_value=CheckResult(registry="openai_company_fallback", found=False, details={})
        )

        checks = await enricher._run_registry_checks(
            inn=None,
            ogrn=None,
            name="Тестовая компания",
            raw_id="raw-1",
        )

    gather_mock.assert_not_awaited()
    mock_rusprofile.safe_check.assert_awaited_once()
    mock_openai_fallback.safe_check.assert_awaited_once()
    assert checks["listorg"]["found"] is False
    assert checks["rusprofile"]["found"] is False
    assert checks["openai_company_fallback"]["found"] is False
    assert checks["fssp"]["error"] == "disabled_by_config"


@pytest.mark.asyncio
async def test_rusprofile_success_skips_openai_fallback():
    enricher = Enricher()

    with (
        patch("src.enrichment.enricher.settings") as mock_settings,
        patch("src.enrichment.enricher._listorg_checker") as mock_listorg,
        patch("src.enrichment.enricher._rusprofile_checker") as mock_rusprofile,
        patch("src.enrichment.enricher._openai_company_fallback_checker") as mock_openai_fallback,
        patch.object(DaDataChecker, "safe_check", new_callable=AsyncMock, return_value=CheckResult(registry="dadata_fns", found=False)),
        patch.object(ArbitrChecker, "safe_check", new_callable=AsyncMock, return_value=CheckResult(registry="kad_arbitr", found=False)),
        patch.object(EfrsbChecker, "safe_check", new_callable=AsyncMock, return_value=CheckResult(registry="efrsb", found=False)),
        patch.object(EisChecker, "safe_check", new_callable=AsyncMock, return_value=CheckResult(registry="eis_zakupki", found=False)),
        patch.object(NostroyChecker, "safe_check", new_callable=AsyncMock, return_value=CheckResult(registry="nostroy", found=False)),
    ):
        mock_settings.listorg_enabled = True
        mock_settings.listorg_primary = True
        mock_settings.listorg_allow_name_lookup = True
        mock_settings.listorg_fallback_enabled = True
        mock_settings.rusprofile_enabled = True
        mock_settings.openai_company_fallback_enabled = True
        mock_settings.enable_fssp_check = False

        mock_listorg.safe_check = AsyncMock(
            return_value=CheckResult(registry="listorg", found=False, details={})
        )
        mock_rusprofile.safe_check = AsyncMock(
            return_value=CheckResult(
                registry="rusprofile",
                found=True,
                status="active",
                details={
                    "name": "ООО Тест",
                    "inn": "5501234567",
                    "ogrn": "1155500001234",
                    "entity_type": "ЮЛ",
                    "source_url": "https://www.rusprofile.ru/id/1",
                },
            )
        )
        mock_openai_fallback.safe_check = AsyncMock(
            return_value=CheckResult(registry="openai_company_fallback", found=False, details={})
        )

        checks = await enricher._run_registry_checks(
            inn=None,
            ogrn=None,
            name="Тестовая компания",
            raw_id="raw-2",
        )

    mock_openai_fallback.safe_check.assert_not_awaited()
    assert checks["rusprofile"]["found"] is True
    assert checks["dadata_fns"]["found"] is True
    assert checks["fssp"]["error"] == "disabled_by_config"


@pytest.mark.asyncio
async def test_avito_light_mode_skips_registry_and_external_review_search():
    enricher = Enricher()
    raw = CompanyRaw(
        id=uuid.uuid4(),
        source="avito",
        source_id="123",
        source_link="https://www.avito.ru/item/123",
        raw_payload={},
        name_raw="Тестовый подрядчик",
        phones=["+79990000000"],
        emails=[],
        addresses=["Омск"],
        contacts_json={},
        inn=None,
        ogrn=None,
        average_rating=4.8,
        reviews_count=10,
        collection_week_start=None,
    )

    with (
        patch("src.enrichment.enricher.settings") as mock_settings,
        patch.object(Enricher, "_enrich_from_website", new_callable=AsyncMock),
        patch.object(Enricher, "_run_registry_checks", new_callable=AsyncMock) as run_checks_mock,
        patch("src.enrichment.enricher._review_searcher") as searcher_mock,
    ):
        mock_settings.avito_light_enrichment = True
        mock_settings.listorg_enabled = True
        mock_settings.listorg_allow_name_lookup = True
        mock_settings.strict_legal_match = True
        mock_settings.confidence_threshold = 70
        searcher_mock.search = AsyncMock(return_value=[])

        enriched, extra_reviews = await enricher.enrich(raw)

    run_checks_mock.assert_not_awaited()
    searcher_mock.search.assert_not_awaited()
    assert extra_reviews == []
    assert enriched.legal_verified is True
    assert enriched.legal_match_method == "avito_light_mode"
    assert enriched.checks["legal_match"]["override_reason"] == "avito_light_mode"


@pytest.mark.asyncio
async def test_registry_person_fields_survive_to_api_detail():
    enricher = Enricher()
    deduplicator = Deduplicator()
    raw = CompanyRaw(
        id=uuid.uuid4(),
        source="yandex",
        source_id="123",
        source_link="https://yandex.ru/maps/org/test/123",
        raw_payload={"relevance": {"confidence": 0.91, "geo_pass": True}},
        name_raw="Тестовая компания",
        phones=["+79990000000"],
        emails=[],
        addresses=["Омск"],
        contacts_json={},
        inn=None,
        ogrn=None,
        average_rating=4.8,
        reviews_count=10,
        collection_week_start=None,
    )
    checks_payload = {
        "listorg": {
            "registry": "listorg",
            "found": True,
            "status": "active",
            "details": {
                "director_name": "Иванов Иван Иванович",
                "director_position": "Генеральный директор",
                "founders": ["Петров Петр Петрович"],
            },
        },
        "dadata_fns": {
            "registry": "dadata_fns",
            "found": True,
            "status": "ACTIVE",
            "details": {
                "name": 'ООО "Тестовая компания"',
                "inn": "5501234567",
                "ogrn": "1155500001234",
                "entity_type": "ЮЛ",
                "source": "listorg",
                "director_name": "Иванов Иван Иванович",
                "director_position": "Генеральный директор",
                "founders": ["Петров Петр Петрович"],
            },
        },
    }

    with (
        patch("src.enrichment.enricher.settings") as mock_settings,
        patch.object(Enricher, "_enrich_from_website", new_callable=AsyncMock),
        patch.object(Enricher, "_run_registry_checks", new_callable=AsyncMock, return_value=checks_payload),
        patch("src.enrichment.enricher._review_searcher") as searcher_mock,
    ):
        mock_settings.avito_light_enrichment = True
        mock_settings.listorg_enabled = True
        mock_settings.listorg_allow_name_lookup = True
        mock_settings.strict_legal_match = True
        mock_settings.confidence_threshold = 70
        searcher_mock.search = AsyncMock(return_value=[])

        enriched, extra_reviews = await enricher.enrich(raw)

    assert extra_reviews == []
    cards = deduplicator.run([(raw, enriched)])
    orm_card = canonical_to_orm(cards[0], summary="ok", risk_level="green", risk_reasons=[], week_start=date(2026, 3, 2))
    now = datetime.now(timezone.utc)
    orm_card.last_updated = now
    orm_card.created_at = now
    api_card = CompanyDetail.model_validate(orm_card)

    assert api_card.checks["dadata_fns"]["details"]["director_name"] == "Иванов Иван Иванович"
    assert api_card.checks["dadata_fns"]["details"]["founders"] == ["Петров Петр Петрович"]
