from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from src.api.routes.companies import delete_company_from_clean
from src.database.models import CompanyClean


class FakeSession:
    def __init__(self, row: CompanyClean | None):
        self._row = row
        self.deleted_row = None
        self.flushed = False

    async def get(self, model, company_id):
        return self._row

    async def delete(self, row):
        self.deleted_row = row

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_delete_company_from_clean_removes_row():
    company_id = uuid.uuid4()
    row = CompanyClean(id=company_id, name_normalized="Test Company")
    session = FakeSession(row)

    result = await delete_company_from_clean(company_id, session)

    assert result.company_id == company_id
    assert result.deleted is True
    assert result.deleted_from == "companies_omsk_clean"
    assert session.deleted_row is row
    assert session.flushed is True


@pytest.mark.asyncio
async def test_delete_company_from_clean_returns_404_for_missing_company():
    company_id = uuid.uuid4()
    session = FakeSession(None)

    with pytest.raises(HTTPException) as exc:
        await delete_company_from_clean(company_id, session)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Company not found"
