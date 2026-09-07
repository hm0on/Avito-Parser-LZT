from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException

from src.api.routes.enriched import get_enriched_company, list_enriched_companies
from src.database.models import CompanyEnriched, CompanyRaw


class _ExecuteResult:
    def __init__(self, *, scalar_one=None, rows=None, first=None):
        self._scalar_one = scalar_one
        self._rows = rows or []
        self._first = first

    def scalar_one(self):
        return self._scalar_one

    def all(self):
        return self._rows

    def first(self):
        return self._first


class FakeSession:
    def __init__(self, *, list_rows=None, total=0, detail_row=None):
        self.list_rows = list_rows or []
        self.total = total
        self.detail_row = detail_row
        self.execute_calls = 0

    async def execute(self, stmt):
        self.execute_calls += 1
        if self.detail_row is not None:
            return _ExecuteResult(first=self.detail_row)
        if self.execute_calls == 1:
            return _ExecuteResult(scalar_one=self.total)
        return _ExecuteResult(rows=self.list_rows)


def _build_pair():
    raw_id = uuid.uuid4()
    enriched_id = uuid.uuid4()
    raw = CompanyRaw(
        id=raw_id,
        source="2gis",
        source_id="firm-1",
        source_link="https://2gis.ru/firm/1",
        name_raw="Тест Сырой",
        collection_week_start=date(2026, 3, 2),
        collected_at=datetime.now(UTC),
        is_processed=True,
    )
    enriched = CompanyEnriched(
        id=enriched_id,
        raw_id=raw_id,
        name_normalized="Тест Норм",
        legal_verified=True,
        legal_match_method="inn",
        legal_match_score=1.0,
        relevance_score=0.91,
        confidence_score=95,
        manual_review_required=False,
        pipeline_week_start=date(2026, 3, 2),
        enriched_at=datetime.now(UTC),
    )
    return enriched, raw


@pytest.mark.asyncio
async def test_list_enriched_companies_returns_paginated_rows():
    enriched, raw = _build_pair()
    session = FakeSession(list_rows=[(enriched, raw)], total=1)

    result = await list_enriched_companies(source=None, page=1, page_size=20, session=session)

    assert result.total == 1
    assert len(result.results) == 1
    item = result.results[0]
    assert item.id == enriched.id
    assert item.raw_id == raw.id
    assert item.source == "2gis"
    assert item.name_normalized == "Тест Норм"


@pytest.mark.asyncio
async def test_get_enriched_company_returns_detail():
    enriched, raw = _build_pair()
    session = FakeSession(detail_row=(enriched, raw))

    result = await get_enriched_company(enriched.id, session)

    assert result.id == enriched.id
    assert result.raw_id == raw.id
    assert result.source == "2gis"
    assert result.name_raw == "Тест Сырой"


@pytest.mark.asyncio
async def test_get_enriched_company_returns_404_for_missing_row():
    session = FakeSession(detail_row=None)

    with pytest.raises(HTTPException) as exc:
        await get_enriched_company(uuid.uuid4(), session)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Enriched company not found"
