"""Reusable query helpers for company API endpoints."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException

from src.database.models import CompanyClean, CompanyRaw

SUPPORTED_COMPANY_SOURCES = ("avito", "2gis", "yandex")


def normalize_company_source(source: str | None) -> str | None:
    if source is None:
        return None

    value = source.strip().lower()
    if not value:
        return None

    if value not in SUPPORTED_COMPANY_SOURCES:
        supported = ", ".join(SUPPORTED_COMPANY_SOURCES)
        raise HTTPException(status_code=400, detail=f"Unsupported source filter: {value}. Expected one of: {supported}")

    return value


def apply_source_filter(stmt, *, source: str | None):
    normalized = normalize_company_source(source)
    if normalized is None:
        return stmt
    return stmt.where(CompanyClean.merged_sources.contains([normalized]))


def apply_company_raw_source_filter(stmt, *, source: str | None):
    normalized = normalize_company_source(source)
    if normalized is None:
        return stmt
    return stmt.where(CompanyRaw.source == normalized)


def extract_source_link(company: CompanyClean, source: str) -> str | None:
    normalized = normalize_company_source(source)
    if normalized is None:
        return None

    source_links = company.source_links or []
    if not isinstance(source_links, Iterable):
        return None

    for item in source_links:
        if not isinstance(item, dict):
            continue
        item_source = str(item.get("source") or "").strip().lower()
        if item_source != normalized:
            continue
        url = str(item.get("url") or "").strip()
        if url:
            return url

    return None
