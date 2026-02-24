"""Company CRUD endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import CompanyDetail, CompanyListItem, ManualReviewPayload, PaginatedCompanies
from src.database.models import CompanyClean
from src.database.session import get_session

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=PaginatedCompanies)
async def list_companies(
    region: str | None = Query(None, description="Фильтр по региону (в адресах)"),
    risk: str | None = Query(None, pattern="^(green|yellow|red)$", description="Уровень риска"),
    min_rating: float | None = Query(None, ge=0.0, le=5.0),
    service: str | None = Query(None, description="Ключевое слово в названии"),
    manual_review: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> PaginatedCompanies:
    stmt = select(CompanyClean)

    if risk:
        stmt = stmt.where(CompanyClean.risk_level == risk)
    if region:
        stmt = stmt.where(cast(CompanyClean.addresses, String).ilike(f"%{region}%"))
    if min_rating is not None:
        stmt = stmt.where(CompanyClean.average_rating >= min_rating)
    if service:
        stmt = stmt.where(CompanyClean.name_normalized.ilike(f"%{service}%"))
    if manual_review is not None:
        stmt = stmt.where(CompanyClean.manual_review_required == manual_review)

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # Paginate
    stmt = stmt.order_by(CompanyClean.last_updated.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    rows = (await session.execute(stmt)).scalars().all()

    return PaginatedCompanies(
        total=total,
        page=page,
        page_size=page_size,
        results=[CompanyListItem.model_validate(r) for r in rows],
    )


@router.get("/{company_id}", response_model=CompanyDetail)
async def get_company(
    company_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CompanyDetail:
    row = await session.get(CompanyClean, company_id)
    if not row:
        raise HTTPException(status_code=404, detail="Company not found")
    return CompanyDetail.model_validate(row)


@router.post("/{company_id}/manual_review", response_model=CompanyDetail)
async def submit_manual_review(
    company_id: uuid.UUID,
    payload: ManualReviewPayload,
    session: AsyncSession = Depends(get_session),
) -> CompanyDetail:
    row = await session.get(CompanyClean, company_id)
    if not row:
        raise HTTPException(status_code=404, detail="Company not found")

    row.risk_level = payload.resolved_risk_level
    if payload.clear_manual_flag:
        row.manual_review_required = False

    # Append reviewer note to risk_reasons
    reasons = list(row.risk_reasons or [])
    reasons.append(f"[Manual review] {payload.reviewer_notes}")
    row.risk_reasons = reasons[:5]

    await session.flush()
    await session.refresh(row)
    return CompanyDetail.model_validate(row)
