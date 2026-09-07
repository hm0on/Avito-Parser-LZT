from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from src.api.routes.raw import get_raw_company, list_raw_company_reviews
from src.database.models import CompanyRaw, ReviewRaw


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecuteResult:
    def __init__(self, *, scalar_one=None, scalars=None):
        self._scalar_one = scalar_one
        self._scalars = scalars or []

    def scalar_one(self):
        return self._scalar_one

    def scalars(self):
        return _ScalarResult(self._scalars)


class FakeSession:
    def __init__(self, *, raw_company: CompanyRaw | None, reviews: list[ReviewRaw] | None = None):
        self.raw_company = raw_company
        self.reviews = reviews or []
        self.execute_calls = 0

    async def get(self, model, raw_company_id):
        if model is CompanyRaw and self.raw_company and self.raw_company.id == raw_company_id:
            return self.raw_company
        return None

    async def execute(self, stmt):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ExecuteResult(scalar_one=len(self.reviews))
        return _ExecuteResult(scalars=self.reviews)


@pytest.mark.asyncio
async def test_get_raw_company_returns_detail():
    raw_id = uuid.uuid4()
    row = CompanyRaw(
        id=raw_id,
        source="2gis",
        name_raw="Тестовая компания",
        collected_at=datetime.now(UTC),
        is_processed=False,
    )
    session = FakeSession(raw_company=row)

    result = await get_raw_company(raw_id, session)

    assert result.id == raw_id
    assert result.source == "2gis"
    assert result.name_raw == "Тестовая компания"


@pytest.mark.asyncio
async def test_get_raw_company_returns_404_for_missing_row():
    session = FakeSession(raw_company=None)

    with pytest.raises(HTTPException) as exc:
        await get_raw_company(uuid.uuid4(), session)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Raw company not found"


@pytest.mark.asyncio
async def test_list_raw_company_reviews_returns_only_selected_company_reviews():
    raw_id = uuid.uuid4()
    row = CompanyRaw(
        id=raw_id,
        source="yandex",
        name_raw="Буровая компания",
        collected_at=datetime.now(UTC),
        is_processed=False,
    )
    reviews = [
        ReviewRaw(id=uuid.uuid4(), company_raw_id=raw_id, source="yandex", text="Отзыв 1"),
        ReviewRaw(id=uuid.uuid4(), company_raw_id=raw_id, source="yandex", text="Отзыв 2"),
    ]
    session = FakeSession(raw_company=row, reviews=reviews)

    result = await list_raw_company_reviews(raw_id, page=1, page_size=20, session=session)

    assert result.total == 2
    assert len(result.results) == 2
    assert [item.text for item in result.results] == ["Отзыв 1", "Отзыв 2"]
