"""Read-only raw data endpoints."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.query_filters import apply_company_raw_source_filter
from src.api.schemas import (
    PaginatedRawCompanies,
    PaginatedRawReviews,
    RawCompanyDetail,
    RawCompanyListItem,
    RawReviewItem,
)
from src.database.models import CompanyRaw, ReviewRaw
from src.database.session import get_session

router = APIRouter(prefix="/raw", tags=["raw"])


@router.get("/weeks", response_model=list[date])
async def list_raw_weeks(
    session: AsyncSession = Depends(get_session),
) -> list[date]:
    stmt = (
        select(CompanyRaw.collection_week_start)
        .where(CompanyRaw.collection_week_start.is_not(None))
        .distinct()
        .order_by(CompanyRaw.collection_week_start.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/companies", response_model=PaginatedRawCompanies)
async def list_raw_companies(
    source: str | None = Query(None, description="Фильтр по источнику: avito, 2gis, yandex"),
    week_start: date | None = Query(None, description="Дата начала недели (понедельник)"),
    latest_week: bool = Query(True, description="Вернуть только записи последней доступной недели"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> PaginatedRawCompanies:
    stmt = select(CompanyRaw)
    stmt = apply_company_raw_source_filter(stmt, source=source)

    if week_start is not None:
        stmt = stmt.where(CompanyRaw.collection_week_start == week_start)
    elif latest_week:
        latest_week_subquery = (
            select(func.max(CompanyRaw.collection_week_start))
            .where(CompanyRaw.collection_week_start.is_not(None))
            .scalar_subquery()
        )
        stmt = stmt.where(CompanyRaw.collection_week_start == latest_week_subquery)

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(CompanyRaw.collected_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()

    return PaginatedRawCompanies(
        total=total,
        page=page,
        page_size=page_size,
        results=[RawCompanyListItem.model_validate(row) for row in rows],
    )


@router.get("/companies/{raw_company_id}", response_model=RawCompanyDetail)
async def get_raw_company(
    raw_company_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RawCompanyDetail:
    row = await session.get(CompanyRaw, raw_company_id)
    if not row:
        raise HTTPException(status_code=404, detail="Raw company not found")
    return RawCompanyDetail.model_validate(row)


@router.get("/companies/{raw_company_id}/reviews", response_model=PaginatedRawReviews)
async def list_raw_company_reviews(
    raw_company_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> PaginatedRawReviews:
    row = await session.get(CompanyRaw, raw_company_id)
    if not row:
        raise HTTPException(status_code=404, detail="Raw company not found")

    stmt = select(ReviewRaw).where(ReviewRaw.company_raw_id == raw_company_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(ReviewRaw.review_date.desc()).offset((page - 1) * page_size).limit(page_size)
    reviews = (await session.execute(stmt)).scalars().all()

    return PaginatedRawReviews(
        total=total,
        page=page,
        page_size=page_size,
        results=[RawReviewItem.model_validate(review) for review in reviews],
    )


@router.get("/reviews", response_model=PaginatedRawReviews)
async def list_raw_reviews(
    source: str | None = Query(None, description="Фильтр по источнику: avito, 2gis, yandex"),
    week_start: date | None = Query(None, description="Дата начала недели (понедельник)"),
    latest_week: bool = Query(True, description="Вернуть только записи последней доступной недели"),
    company_raw_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> PaginatedRawReviews:
    stmt = select(ReviewRaw).join(CompanyRaw, CompanyRaw.id == ReviewRaw.company_raw_id)

    if company_raw_id is not None:
        stmt = stmt.where(ReviewRaw.company_raw_id == company_raw_id)
    stmt = apply_company_raw_source_filter(stmt, source=source)

    if week_start is not None:
        stmt = stmt.where(CompanyRaw.collection_week_start == week_start)
    elif latest_week:
        latest_week_subquery = (
            select(func.max(CompanyRaw.collection_week_start))
            .where(CompanyRaw.collection_week_start.is_not(None))
            .scalar_subquery()
        )
        stmt = stmt.where(CompanyRaw.collection_week_start == latest_week_subquery)

    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(ReviewRaw.review_date.desc()).offset((page - 1) * page_size).limit(page_size)
    reviews = (await session.execute(stmt)).scalars().all()

    return PaginatedRawReviews(
        total=total,
        page=page,
        page_size=page_size,
        results=[RawReviewItem.model_validate(review) for review in reviews],
    )
