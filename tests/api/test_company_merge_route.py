from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from src.api.routes import companies as companies_module
from src.api.routes.companies import merge_companies
from src.api.schemas import MergeCompaniesPayload
from src.database.models import CompanyClean, CompanyMergeGroup
from src.pipeline.clean_builder import CleanRebuildResult


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecuteResult:
    def __init__(self, *, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        return _ScalarResult(self._scalars)


class FakeSession:
    def __init__(self, rows: list[CompanyClean], master_row: CompanyClean):
        self.rows = rows
        self.master_row = master_row
        self.refreshed = False

    async def execute(self, stmt):
        return _ExecuteResult(scalars=self.rows)

    async def get(self, model, company_id):
        if company_id == self.master_row.id:
            return self.master_row
        for row in self.rows:
            if row.id == company_id:
                return row
        return None

    async def refresh(self, row):
        self.refreshed = True


@pytest.mark.asyncio
async def test_merge_companies_returns_master_card(monkeypatch):
    week_start = date(2026, 3, 2)
    company_a = CompanyClean(
        id=uuid.uuid4(),
        identity_key="2gis:id:1",
        name_normalized="Компания A",
        legal_verified=False,
        geo_verified=False,
        manual_review_required=False,
        pipeline_week_start=week_start,
        last_updated=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    company_b = CompanyClean(
        id=uuid.uuid4(),
        identity_key="yandex:id:2",
        name_normalized="Компания B",
        legal_verified=False,
        geo_verified=False,
        manual_review_required=False,
        pipeline_week_start=week_start,
        last_updated=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    master_id = uuid.uuid4()
    master_row = CompanyClean(
        id=master_id,
        identity_key="merge-group:test",
        merge_group_id=uuid.uuid4(),
        name_normalized="Компания Master",
        merged_sources=["2gis", "yandex"],
        legal_verified=False,
        geo_verified=False,
        manual_review_required=False,
        pipeline_week_start=week_start,
        last_updated=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    session = FakeSession([company_a, company_b], master_row)

    async def _fake_create_or_update_manual_merge(session_obj, *, rows, primary_company_id):
        assert rows == [company_a, company_b]
        assert primary_company_id == company_a.id
        return CompanyMergeGroup(id=uuid.uuid4(), master_company_id=master_id, primary_identity_key=company_a.identity_key)

    async def _fake_rebuild_clean_rows_for_week(session_obj, *, week_start, deduplicator, risk_assessor):
        assert week_start == date(2026, 3, 2)
        return CleanRebuildResult(rows=[master_row], total_reviews=0, source_total=2)

    monkeypatch.setattr(companies_module, "_create_or_update_manual_merge", _fake_create_or_update_manual_merge)
    monkeypatch.setattr(companies_module, "rebuild_clean_rows_for_week", _fake_rebuild_clean_rows_for_week)

    result = await merge_companies(
        MergeCompaniesPayload(company_ids=[company_a.id, company_b.id], primary_company_id=company_a.id),
        session,
    )

    assert result.master_company_id == master_id
    assert result.merged_company_ids == [company_a.id, company_b.id]
    assert result.company.id == master_id
    assert session.refreshed is True
