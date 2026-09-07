from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from src.api.routes import companies as companies_module
from src.api.routes.companies import summarize_company_reviews
from src.api.schemas import SummarizeReviewsPayload
from src.database.models import CompanyClean, ReviewRaw


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
    def __init__(self, row: CompanyClean, reviews: list[ReviewRaw]):
        self.row = row
        self.reviews = reviews
        self.flushed = False
        self.refreshed = False

    async def get(self, model, company_id):
        return self.row if self.row.id == company_id else None

    async def execute(self, stmt):
        return _ExecuteResult(scalars=self.reviews)

    async def flush(self):
        self.flushed = True

    async def refresh(self, row):
        self.refreshed = True


@pytest.mark.asyncio
async def test_summarize_company_reviews_persists_manual_summary(monkeypatch):
    company_id = uuid.uuid4()
    raw_id = uuid.uuid4()
    row = CompanyClean(
        id=company_id,
        identity_key="yandex:id:123",
        name_normalized="Тест Компания",
        source_records=[str(raw_id)],
        summary_review=None,
        legal_verified=False,
        geo_verified=False,
        manual_review_required=False,
        pipeline_week_start=date(2026, 3, 2),
        last_updated=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    reviews = [
        ReviewRaw(id=uuid.uuid4(), company_raw_id=raw_id, source="yandex", text="Хорошо"),
        ReviewRaw(id=uuid.uuid4(), company_raw_id=raw_id, source="yandex", text="Отлично"),
    ]
    session = FakeSession(row, reviews)
    captured: dict[str, str] = {}

    async def _fake_summarize(company_name, reviews_payload):
        assert company_name == "Тест Компания"
        assert len(reviews_payload) == 2
        return "Ручное summary"

    async def _fake_upsert(session_obj, *, identity_key, summary_review):
        captured[identity_key] = summary_review
        return None

    monkeypatch.setattr(companies_module._summarizer, "summarize", _fake_summarize)
    monkeypatch.setattr(companies_module, "upsert_summary_override", _fake_upsert)

    result = await summarize_company_reviews(
        company_id,
        SummarizeReviewsPayload(force=False),
        session,
    )

    assert result.summary_review == "Ручное summary"
    assert row.summary_review == "Ручное summary"
    assert captured == {"yandex:id:123": "Ручное summary"}
    assert session.flushed is True
    assert session.refreshed is True


@pytest.mark.asyncio
async def test_summarize_company_reviews_returns_existing_summary_without_recompute(monkeypatch):
    company_id = uuid.uuid4()
    row = CompanyClean(
        id=company_id,
        identity_key="avito:id:1",
        name_normalized="Тест",
        summary_review="Уже посчитано",
        legal_verified=False,
        geo_verified=False,
        manual_review_required=False,
        source_records=[],
        pipeline_week_start=date(2026, 3, 2),
        last_updated=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    session = FakeSession(row, [])

    async def _boom(*args, **kwargs):
        raise AssertionError("summarizer must not be called")

    monkeypatch.setattr(companies_module._summarizer, "summarize", _boom)

    result = await summarize_company_reviews(
        company_id,
        SummarizeReviewsPayload(force=False),
        session,
    )

    assert result.summary_review == "Уже посчитано"
    assert session.flushed is False
    assert session.refreshed is False
