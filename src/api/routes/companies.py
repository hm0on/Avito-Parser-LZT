"""Company CRUD endpoints."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.risk_assessor import RiskAssessor
from src.ai.summarizer import ReviewSummarizer
from src.api.query_filters import apply_source_filter, extract_source_link
from src.api.schemas import (
    AvitoPhoneLookupResponse,
    CompanyDeleteResponse,
    CompanyDetail,
    CompanyListItem,
    CompanyMergeResponse,
    ManualReviewPayload,
    MergeCompaniesPayload,
    PaginatedCompanies,
    SummarizeReviewsPayload,
)
from src.database.models import CompanyClean, CompanyMergeGroup, CompanyMergeMember, ReviewRaw
from src.database.session import get_session
from src.deduplication.deduplicator import Deduplicator
from src.pipeline.clean_builder import (
    rebuild_clean_rows_for_week,
    load_merge_state,
    upsert_summary_override,
)
from src.pipeline.common import rv_to_dict
from src.services.spfa import SpfaClient, SpfaConfigError, SpfaLookupError, extract_avito_ad_id

router = APIRouter(prefix="/companies", tags=["companies"])
_spfa_client = SpfaClient()
_summarizer = ReviewSummarizer()
_deduplicator = Deduplicator()
_risk_assessor = RiskAssessor()


async def _get_company_or_404(session: AsyncSession, company_id: uuid.UUID) -> CompanyClean:
    row = await session.get(CompanyClean, company_id)
    if not row:
        raise HTTPException(status_code=404, detail="Company not found")
    return row


async def _load_all_reviews_for_company(session: AsyncSession, row: CompanyClean) -> list[dict]:
    source_records = []
    for raw_id in row.source_records or []:
        try:
            source_records.append(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError):
            continue

    if not source_records:
        return []

    stmt = (
        select(ReviewRaw)
        .where(ReviewRaw.company_raw_id.in_(list(dict.fromkeys(source_records))))
        .order_by(ReviewRaw.review_date.desc(), ReviewRaw.id.asc())
    )
    reviews = (await session.execute(stmt)).scalars().all()
    return [rv_to_dict(review) for review in reviews]


async def _create_or_update_manual_merge(
    session: AsyncSession,
    *,
    rows: list[CompanyClean],
    primary_company_id: uuid.UUID | None,
) -> CompanyMergeGroup:
    merge_state = await load_merge_state(session)
    row_by_id = {row.id: row for row in rows}

    if primary_company_id is not None and primary_company_id not in row_by_id:
        raise HTTPException(status_code=422, detail="primary_company_id must be one of company_ids")

    involved_group_ids: list[uuid.UUID] = []
    target_identities: set[str] = set()

    for row in rows:
        group_id = row.merge_group_id
        if group_id is None and row.identity_key:
            group_id = merge_state.group_by_identity.get(row.identity_key)

        if group_id is not None:
            involved_group_ids.append(group_id)
            target_identities.update(merge_state.members_by_group.get(group_id, []))
            continue

        if not row.identity_key:
            raise HTTPException(status_code=422, detail="Selected company has no stable identity_key")
        target_identities.add(row.identity_key)

    if len(target_identities) < 2:
        raise HTTPException(status_code=422, detail="At least two distinct companies are required for merge")

    primary_identity_key: str | None = None
    if primary_company_id is not None:
        primary_row = row_by_id[primary_company_id]
        primary_group_id = primary_row.merge_group_id
        if primary_group_id is None and primary_row.identity_key:
            primary_group_id = merge_state.group_by_identity.get(primary_row.identity_key)
        if primary_group_id is not None:
            existing_primary = merge_state.groups.get(primary_group_id)
            primary_identity_key = existing_primary.primary_identity_key if existing_primary else None
            if primary_identity_key is None:
                members = merge_state.members_by_group.get(primary_group_id, [])
                primary_identity_key = members[0] if members else primary_row.identity_key
        else:
            primary_identity_key = primary_row.identity_key
    else:
        for row in rows:
            group_id = row.merge_group_id
            if group_id is None and row.identity_key:
                group_id = merge_state.group_by_identity.get(row.identity_key)
            if group_id is None:
                continue
            group = merge_state.groups.get(group_id)
            if group and group.primary_identity_key:
                primary_identity_key = group.primary_identity_key
                break

    unique_group_ids = list(dict.fromkeys(involved_group_ids))
    target_group: CompanyMergeGroup | None = None

    if unique_group_ids:
        if primary_company_id is not None:
            primary_row = row_by_id[primary_company_id]
            preferred_group_id = primary_row.merge_group_id
            if preferred_group_id is None and primary_row.identity_key:
                preferred_group_id = merge_state.group_by_identity.get(primary_row.identity_key)
            if preferred_group_id is not None:
                target_group = merge_state.groups.get(preferred_group_id)
        if target_group is None:
            target_group = merge_state.groups[unique_group_ids[0]]
    else:
        target_group = CompanyMergeGroup(master_company_id=uuid.uuid4())
        session.add(target_group)
        await session.flush()

    assert target_group is not None

    member_rows_by_group: dict[uuid.UUID, list[CompanyMergeMember]] = {}
    if unique_group_ids:
        stmt = select(CompanyMergeMember).where(CompanyMergeMember.group_id.in_(unique_group_ids))
        for member in (await session.execute(stmt)).scalars().all():
            member_rows_by_group.setdefault(member.group_id, []).append(member)

    target_member_rows = member_rows_by_group.get(target_group.id, [])
    target_member_identities = {member.identity_key for member in target_member_rows}

    for group_id in unique_group_ids:
        if group_id == target_group.id:
            continue
        for member in member_rows_by_group.get(group_id, []):
            if member.identity_key in target_member_identities:
                await session.delete(member)
                continue
            member.group_id = target_group.id
            target_member_identities.add(member.identity_key)

        group = merge_state.groups.get(group_id)
        if group is not None:
            await session.delete(group)

    for identity_key in sorted(target_identities):
        if identity_key in target_member_identities:
            continue
        session.add(CompanyMergeMember(group_id=target_group.id, identity_key=identity_key))
        target_member_identities.add(identity_key)

    target_group.primary_identity_key = primary_identity_key or target_group.primary_identity_key or sorted(target_member_identities)[0]
    await session.flush()
    return target_group


@router.get("", response_model=PaginatedCompanies)
async def list_companies(
    region: str | None = Query(None, description="Фильтр по региону (в адресах)"),
    risk: str | None = Query(None, pattern="^(green|yellow|red)$", description="Уровень риска"),
    min_rating: float | None = Query(None, ge=0.0, le=5.0),
    service: str | None = Query(None, description="Ключевое слово в названии"),
    source: str | None = Query(
        None,
        description="Фильтр по источнику компании: avito, 2gis, yandex",
    ),
    manual_review: bool | None = Query(None),
    legal_verified: bool | None = Query(
        True,
        description="Только юридически верифицированные компании (default=true)",
    ),
    min_relevance_score: float | None = Query(
        None,
        ge=0.0,
        le=1.0,
        description="Минимальный relevance_score (0..1)",
    ),
    geo_verified: bool | None = Query(
        None,
        description="Фильтр по geo-верификации",
    ),
    week_start: date | None = Query(
        None,
        description="Дата начала недели (понедельник) в формате YYYY-MM-DD",
    ),
    latest_week: bool = Query(
        True,
        description="Вернуть только записи последней доступной недели",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> PaginatedCompanies:
    stmt = select(CompanyClean)

    if risk:
        stmt = stmt.where(CompanyClean.risk_level == risk)
    if min_rating is not None:
        stmt = stmt.where(CompanyClean.average_rating >= min_rating)
    if service:
        stmt = stmt.where(CompanyClean.name_normalized.ilike(f"%{service}%"))
    if manual_review is not None:
        stmt = stmt.where(CompanyClean.manual_review_required == manual_review)
    if legal_verified is not None:
        stmt = stmt.where(CompanyClean.legal_verified == legal_verified)
    stmt = apply_source_filter(stmt, source=source)
    if min_relevance_score is not None:
        stmt = stmt.where(CompanyClean.relevance_score.is_not(None))
        stmt = stmt.where(CompanyClean.relevance_score >= min_relevance_score)
    if geo_verified is not None:
        stmt = stmt.where(CompanyClean.geo_verified == geo_verified)
    if week_start is not None:
        stmt = stmt.where(CompanyClean.pipeline_week_start == week_start)
    elif latest_week:
        latest_week_subquery = (
            select(func.max(CompanyClean.pipeline_week_start))
            .where(CompanyClean.pipeline_week_start.is_not(None))
            .scalar_subquery()
        )
        stmt = stmt.where(CompanyClean.pipeline_week_start == latest_week_subquery)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(CompanyClean.last_updated.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    rows = (await session.execute(stmt)).scalars().all()

    return PaginatedCompanies(
        total=total,
        page=page,
        page_size=page_size,
        results=[CompanyListItem.model_validate(r) for r in rows],
    )


@router.get("/weeks", response_model=list[date])
async def list_available_weeks(
    session: AsyncSession = Depends(get_session),
) -> list[date]:
    stmt = (
        select(CompanyClean.pipeline_week_start)
        .where(CompanyClean.pipeline_week_start.is_not(None))
        .distinct()
        .order_by(CompanyClean.pipeline_week_start.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


@router.post("/merge", response_model=CompanyMergeResponse)
async def merge_companies(
    payload: MergeCompaniesPayload,
    session: AsyncSession = Depends(get_session),
) -> CompanyMergeResponse:
    company_ids = list(dict.fromkeys(payload.company_ids))
    if len(company_ids) < 2:
        raise HTTPException(status_code=422, detail="At least two unique company_ids are required")

    stmt = select(CompanyClean).where(CompanyClean.id.in_(company_ids))
    rows = (await session.execute(stmt)).scalars().all()
    if len(rows) != len(company_ids):
        found_ids = {row.id for row in rows}
        missing = [str(company_id) for company_id in company_ids if company_id not in found_ids]
        raise HTTPException(status_code=404, detail=f"Companies not found: {', '.join(missing)}")

    week_starts = {row.pipeline_week_start for row in rows}
    if len(week_starts) != 1:
        raise HTTPException(status_code=422, detail="All companies must belong to the same pipeline week")

    week_start = next(iter(week_starts))
    if week_start is None:
        raise HTTPException(status_code=422, detail="Selected companies are missing pipeline_week_start")

    target_group = await _create_or_update_manual_merge(
        session,
        rows=rows,
        primary_company_id=payload.primary_company_id,
    )

    await rebuild_clean_rows_for_week(
        session,
        week_start=week_start,
        deduplicator=_deduplicator,
        risk_assessor=_risk_assessor,
    )

    master_row = await session.get(CompanyClean, target_group.master_company_id)
    if not master_row:
        raise HTTPException(status_code=409, detail="Manual merge was saved, but master company was not rebuilt")

    await session.refresh(master_row)
    return CompanyMergeResponse(
        master_company_id=master_row.id,
        merged_company_ids=company_ids,
        company=CompanyDetail.model_validate(master_row),
    )


@router.get("/{company_id}", response_model=CompanyDetail)
async def get_company(
    company_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CompanyDetail:
    row = await _get_company_or_404(session, company_id)
    return CompanyDetail.model_validate(row)


@router.post("/{company_id}/delete", response_model=CompanyDeleteResponse)
async def delete_company_from_clean(
    company_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CompanyDeleteResponse:
    row = await _get_company_or_404(session, company_id)

    await session.delete(row)
    await session.flush()
    return CompanyDeleteResponse(company_id=company_id)


@router.post("/{company_id}/avito_phone", response_model=AvitoPhoneLookupResponse)
async def lookup_avito_phone(
    company_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> AvitoPhoneLookupResponse:
    row = await _get_company_or_404(session, company_id)

    avito_url = extract_source_link(row, "avito")
    if not avito_url:
        raise HTTPException(status_code=404, detail="Avito source link not found for this company")
    ad_id = extract_avito_ad_id(avito_url)
    if not ad_id:
        raise HTTPException(status_code=422, detail="Could not extract Avito ad id from source link")

    try:
        result = await _spfa_client.lookup_phone_by_ad_id(ad_id)
    except SpfaConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SpfaLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return AvitoPhoneLookupResponse(
        company_id=row.id,
        avito_url=avito_url,
        ad_id=result.ad_id,
        phone=result.phone,
        provider_status=result.provider_status,
        raw_response=result.raw_response,
    )


@router.post("/{company_id}/summarize_reviews", response_model=CompanyDetail)
async def summarize_company_reviews(
    company_id: uuid.UUID,
    payload: SummarizeReviewsPayload,
    session: AsyncSession = Depends(get_session),
) -> CompanyDetail:
    row = await _get_company_or_404(session, company_id)

    if row.summary_review and not payload.force:
        return CompanyDetail.model_validate(row)
    if not row.identity_key:
        raise HTTPException(status_code=422, detail="Company has no stable identity key")

    reviews = await _load_all_reviews_for_company(session, row)
    if not reviews:
        raise HTTPException(status_code=422, detail="No reviews available for summarization")

    summary = await _summarizer.summarize(row.name_normalized, reviews)
    if not summary:
        raise HTTPException(status_code=502, detail="Review summarization failed")

    await upsert_summary_override(
        session,
        identity_key=row.identity_key,
        summary_review=summary,
    )
    row.summary_review = summary
    await session.flush()
    await session.refresh(row)
    return CompanyDetail.model_validate(row)


@router.post("/{company_id}/manual_review", response_model=CompanyDetail)
async def submit_manual_review(
    company_id: uuid.UUID,
    payload: ManualReviewPayload,
    session: AsyncSession = Depends(get_session),
) -> CompanyDetail:
    row = await _get_company_or_404(session, company_id)

    row.risk_level = payload.resolved_risk_level
    if payload.clear_manual_flag:
        row.manual_review_required = False

    reasons = list(row.risk_reasons or [])
    reasons.append(f"[Manual review] {payload.reviewer_notes}")
    row.risk_reasons = reasons[:5]

    await session.flush()
    await session.refresh(row)
    return CompanyDetail.model_validate(row)
