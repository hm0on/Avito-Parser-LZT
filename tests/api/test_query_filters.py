from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.api.query_filters import (
    apply_company_raw_source_filter,
    apply_source_filter,
    extract_source_link,
    normalize_company_source,
)
from src.database.models import CompanyClean, CompanyRaw


def test_normalize_company_source_accepts_supported_values():
    assert normalize_company_source("avito") == "avito"
    assert normalize_company_source(" YANDEX ") == "yandex"
    assert normalize_company_source("2gis") == "2gis"


def test_normalize_company_source_rejects_unknown_value():
    with pytest.raises(HTTPException) as exc:
        normalize_company_source("flamp")

    assert exc.value.status_code == 400
    assert "Unsupported source filter" in exc.value.detail


def test_apply_source_filter_builds_jsonb_contains_clause():
    stmt = apply_source_filter(select(CompanyClean), source="avito")
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "@>" in compiled
    assert "merged_sources" in compiled


def test_apply_company_raw_source_filter_builds_source_where_clause():
    stmt = apply_company_raw_source_filter(select(CompanyRaw), source="yandex")
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "companies_raw_omsk" in compiled
    assert "source" in compiled


def test_extract_source_link_returns_expected_url():
    company = CompanyClean(
        id=uuid.uuid4(),
        name_normalized="Тест",
        source_name_primary="Тест",
        legal_verified=True,
        geo_verified=True,
        merged_sources=["avito", "2gis"],
        source_links=[
            {"source": "2gis", "url": "https://2gis.ru/test"},
            {"source": "avito", "url": "https://www.avito.ru/test"},
        ],
    )

    assert extract_source_link(company, "avito") == "https://www.avito.ru/test"
    assert extract_source_link(company, "yandex") is None
