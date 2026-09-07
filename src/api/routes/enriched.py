"""Read-only enriched data endpoints."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.query_filters import apply_company_raw_source_filter
from src.api.schemas import (
    EnrichedCompanyDetail,
    EnrichedCompanyListItem,
    PaginatedEnrichedCompanies,
)
from src.database.models import CompanyEnriched, CompanyRaw
from src.database.session import get_session

router = APIRouter(prefix="/enriched", tags=["enriched"])


def _build_enriched_list_item(raw: CompanyRaw, enriched: CompanyEnriched) -> EnrichedCompanyListItem:
    return EnrichedCompanyListItem(
        id=enriched.id,
        raw_id=raw.id,
        source=raw.source,
        source_id=raw.source_id,
        source_link=raw.source_link,
        name_raw=raw.name_raw,
        name_normalized=enriched.name_normalized,
        legal_verified=enriched.legal_verified,
        legal_match_method=enriched.legal_match_method,
        legal_match_score=enriched.legal_match_score,
        relevance_score=enriched.relevance_score,
        confidence_score=enriched.confidence_score,
        manual_review_required=enriched.manual_review_required,
        pipeline_week_start=enriched.pipeline_week_start,
        enriched_at=enriched.enriched_at,
    )


def _build_enriched_detail(raw: CompanyRaw, enriched: CompanyEnriched) -> EnrichedCompanyDetail:
    return EnrichedCompanyDetail(
        id=enriched.id,
        raw_id=raw.id,
        source=raw.source,
        source_id=raw.source_id,
        source_link=raw.source_link,
        raw_payload=raw.raw_payload,
        name_raw=raw.name_raw,
        raw_phones=raw.phones,
        raw_emails=raw.emails,
        raw_addresses=raw.addresses,
        raw_contacts_json=raw.contacts_json,
        raw_inn=raw.inn,
        raw_ogrn=raw.ogrn,
        raw_average_rating=raw.average_rating,
        raw_reviews_count=raw.reviews_count,
        collection_week_start=raw.collection_week_start,
        name_normalized=enriched.name_normalized,
        inn=enriched.inn,
        ogrn=enriched.ogrn,
        entity_type=enriched.entity_type,
        phones_normalized=enriched.phones_normalized,
        addresses_parsed=enriched.addresses_parsed,
        checks=enriched.checks,
        legal_verified=enriched.legal_verified,
        legal_match_method=enriched.legal_match_method,
        legal_match_score=enriched.legal_match_score,
        relevance_score=enriched.relevance_score,
        relevance_details=enriched.relevance_details,
        confidence_score=enriched.confidence_score,
        manual_review_required=enriched.manual_review_required,
        pipeline_week_start=enriched.pipeline_week_start,
        enriched_at=enriched.enriched_at,
    )


@router.get("/weeks", response_model=list[date])
async def list_enriched_weeks(
    session: AsyncSession = Depends(get_session),
) -> list[date]:
    stmt = (
        select(CompanyEnriched.pipeline_week_start)
        .where(CompanyEnriched.pipeline_week_start.is_not(None))
        .distinct()
        .order_by(CompanyEnriched.pipeline_week_start.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/companies", response_model=PaginatedEnrichedCompanies)
async def list_enriched_companies(
    source: str | None = Query(None, description="Фильтр по источнику: avito, 2gis, yandex"),
    legal_verified: bool | None = Query(None),
    manual_review: bool | None = Query(None),
    min_relevance_score: float | None = Query(None, ge=0.0, le=1.0),
    week_start: date | None = Query(None, description="Дата начала недели (понедельник)"),
    latest_week: bool = Query(True, description="Вернуть только записи последней доступной недели"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> PaginatedEnrichedCompanies:
    stmt = select(CompanyEnriched, CompanyRaw).join(CompanyRaw, CompanyRaw.id == CompanyEnriched.raw_id)
    stmt = apply_company_raw_source_filter(stmt, source=source)

    if legal_verified is not None:
        stmt = stmt.where(CompanyEnriched.legal_verified == legal_verified)
    if manual_review is not None:
        stmt = stmt.where(CompanyEnriched.manual_review_required == manual_review)
    if min_relevance_score is not None:
        stmt = stmt.where(CompanyEnriched.relevance_score.is_not(None))
        stmt = stmt.where(CompanyEnriched.relevance_score >= min_relevance_score)
    if week_start is not None:
        stmt = stmt.where(CompanyEnriched.pipeline_week_start == week_start)
    elif latest_week:
        latest_week_subquery = (
            select(func.max(CompanyEnriched.pipeline_week_start))
            .where(CompanyEnriched.pipeline_week_start.is_not(None))
            .scalar_subquery()
        )
        stmt = stmt.where(CompanyEnriched.pipeline_week_start == latest_week_subquery)

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(CompanyEnriched.enriched_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).all()

    return PaginatedEnrichedCompanies(
        total=total,
        page=page,
        page_size=page_size,
        results=[_build_enriched_list_item(raw, enriched) for enriched, raw in rows],
    )


@router.get("/companies/{enriched_id}", response_model=EnrichedCompanyDetail)
async def get_enriched_company(
    enriched_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EnrichedCompanyDetail:
    stmt = (
        select(CompanyEnriched, CompanyRaw)
        .join(CompanyRaw, CompanyRaw.id == CompanyEnriched.raw_id)
        .where(CompanyEnriched.id == enriched_id)
    )
    row = (await session.execute(stmt)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Enriched company not found")

    enriched, raw = row
    return _build_enriched_detail(raw, enriched)
