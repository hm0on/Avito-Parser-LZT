"""Shared helpers for weekly collection and enrichment runners."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.collectors.base import RawCompany, RawReview
from src.config import settings
from src.database.models import (
    CollectionCheckpoint,
    CompanyClean,
    CompanyEnriched,
    CompanyRaw,
    ReviewRaw,
)
from src.deduplication.deduplicator import CanonicalCard

log = structlog.get_logger(__name__)

REQUIRED_COLLECTION_SOURCES = ("avito", "yandex", "2gis")
EXTRA_REVIEW_SOURCES = ("flamp", "otzovik", "vk")


def current_pipeline_week_start(reference: datetime | None = None) -> date:
    now = reference or datetime.now(UTC)
    current_date = now.date()
    return current_date - timedelta(days=current_date.weekday())


async def mark_checkpoint(
    session: AsyncSession,
    *,
    source: str,
    week_start: date,
    status: str,
    companies_count: int | None = None,
    reviews_count: int | None = None,
    error_text: str | None = None,
    details_json: dict | None = None,
) -> CollectionCheckpoint:
    checkpoint = await _get_or_create_checkpoint(session, source=source, week_start=week_start)
    checkpoint.status = status
    checkpoint.error_text = error_text
    checkpoint.details_json = details_json

    if companies_count is not None:
        checkpoint.companies_count = companies_count
    if reviews_count is not None:
        checkpoint.reviews_count = reviews_count

    now = datetime.now(UTC)
    if status == "running":
        if checkpoint.started_at is None:
            checkpoint.started_at = now
        checkpoint.completed_at = None
    elif status == "completed":
        if checkpoint.started_at is None:
            checkpoint.started_at = now
        checkpoint.completed_at = now
    elif status == "failed":
        checkpoint.completed_at = None
    elif status == "pending":
        checkpoint.started_at = None
        checkpoint.completed_at = None

    await session.flush()
    return checkpoint


async def reset_enrichment_checkpoint(
    session: AsyncSession,
    *,
    week_start: date,
    reason: str,
) -> None:
    await mark_checkpoint(
        session,
        source="enrichment",
        week_start=week_start,
        status="pending",
        companies_count=0,
        reviews_count=0,
        error_text=None,
        details_json={"reason": reason},
    )


async def required_sources_completed(
    session: AsyncSession,
    *,
    week_start: date,
) -> bool:
    stmt = select(CollectionCheckpoint).where(CollectionCheckpoint.week_start == week_start)
    checkpoints = {row.source: row for row in (await session.execute(stmt)).scalars().all()}

    missing: list[str] = []
    empty: list[str] = []
    for source in REQUIRED_COLLECTION_SOURCES:
        checkpoint = checkpoints.get(source)
        if checkpoint is None or checkpoint.status != "completed":
            missing.append(source)
            continue
        if (checkpoint.companies_count or 0) <= 0:
            empty.append(source)

    if missing or empty:
        log.info(
            "pipeline.weekly.waiting_sources",
            week_start=str(week_start),
            missing=missing,
            empty_completed=empty,
        )
        return False
    return True


async def load_checkpoint_statuses(
    session: AsyncSession,
    *,
    week_start: date,
) -> dict[str, str]:
    stmt = select(CollectionCheckpoint).where(CollectionCheckpoint.week_start == week_start)
    rows = (await session.execute(stmt)).scalars().all()
    return {row.source: row.status for row in rows}


async def persist_source_collection(
    session: AsyncSession,
    *,
    source: str,
    week_start: date,
    raw_companies: list[RawCompany],
) -> dict[str, int]:
    await session.execute(
        delete(CompanyRaw).where(
            CompanyRaw.source == source,
            CompanyRaw.collection_week_start == week_start,
        )
    )
    await session.flush()

    companies_count = 0
    reviews_count = 0
    for raw_company in raw_companies:
        db_raw = raw_company_to_orm(raw_company, week_start=week_start)
        session.add(db_raw)
        companies_count += 1

        for review in raw_company.reviews:
            session.add(
                ReviewRaw(
                    id=uuid.uuid4(),
                    company_raw_id=db_raw.id,
                    source=review.source,
                    text=review.text,
                    rating=review.rating,
                    author=review.author,
                    review_date=review.review_date,
                    source_link=review.source_link,
                )
            )
            reviews_count += 1

    await session.flush()
    return {
        "companies_count": companies_count,
        "reviews_count": reviews_count,
    }


async def clear_source_collection(
    session: AsyncSession,
    *,
    source: str,
    week_start: date,
) -> None:
    await session.execute(
        delete(CompanyRaw).where(
            CompanyRaw.source == source,
            CompanyRaw.collection_week_start == week_start,
        )
    )
    await session.flush()


async def append_source_collection(
    session: AsyncSession,
    *,
    week_start: date,
    raw_companies: list[RawCompany],
) -> dict[str, int]:
    companies_count = 0
    reviews_count = 0
    for raw_company in raw_companies:
        db_raw = raw_company_to_orm(raw_company, week_start=week_start)
        session.add(db_raw)
        companies_count += 1

        for review in raw_company.reviews:
            session.add(
                ReviewRaw(
                    id=uuid.uuid4(),
                    company_raw_id=db_raw.id,
                    source=review.source,
                    text=review.text,
                    rating=review.rating,
                    author=review.author,
                    review_date=review.review_date,
                    source_link=review.source_link,
                )
            )
            reviews_count += 1

    await session.flush()
    return {
        "companies_count": companies_count,
        "reviews_count": reviews_count,
    }


async def load_week_raw_records(
    session: AsyncSession,
    *,
    week_start: date,
) -> list[CompanyRaw]:
    stmt = (
        select(CompanyRaw)
        .options(selectinload(CompanyRaw.reviews))
        .where(CompanyRaw.collection_week_start == week_start)
        .order_by(CompanyRaw.collected_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def cleanup_week_enrichment_outputs(
    session: AsyncSession,
    *,
    week_start: date,
) -> None:
    raw_id_subquery = select(CompanyRaw.id).where(CompanyRaw.collection_week_start == week_start)

    await session.execute(
        delete(ReviewRaw).where(
            ReviewRaw.company_raw_id.in_(raw_id_subquery),
            ReviewRaw.source.in_(EXTRA_REVIEW_SOURCES),
        )
    )
    await session.execute(
        delete(CompanyEnriched).where(CompanyEnriched.pipeline_week_start == week_start)
    )
    await session.execute(
        delete(CompanyClean).where(CompanyClean.pipeline_week_start == week_start)
    )
    await session.execute(
        update(CompanyRaw)
        .where(CompanyRaw.collection_week_start == week_start)
        .values(is_processed=False)
    )
    await session.flush()


def raw_company_to_orm(raw_company: RawCompany, *, week_start: date) -> CompanyRaw:
    return CompanyRaw(
        id=uuid.uuid4(),
        source=raw_company.source,
        source_id=raw_company.source_id,
        source_link=raw_company.source_link,
        raw_payload=raw_company.raw_payload,
        name_raw=raw_company.name_raw,
        phones=raw_company.phones or [],
        emails=raw_company.emails or [],
        addresses=raw_company.addresses or [],
        contacts_json=raw_company.contacts_json or {},
        inn=raw_company.inn,
        ogrn=raw_company.ogrn,
        average_rating=raw_company.average_rating,
        reviews_count=raw_company.reviews_count,
        collection_week_start=week_start,
        collected_at=raw_company.collected_at,
        is_processed=False,
    )


def rv_to_dict(review: RawReview | ReviewRaw) -> dict:
    return {
        "text": review.text,
        "rating": review.rating,
        "author": review.author,
        "review_date": review.review_date.isoformat() if review.review_date else None,
        "source_link": review.source_link,
        "source": review.source,
    }


def gather_reviews(
    card: CanonicalCard,
    db_id_map: dict[str, CompanyRaw],
    extra_reviews_map: dict[str, list[RawReview]],
) -> list[dict]:
    source_ids = set(card.source_records)
    reviews: list[dict] = []
    stage1_count = 0
    stage2_count = 0

    for raw_id in source_ids:
        db_raw = db_id_map.get(raw_id)
        if db_raw:
            for review in db_raw.reviews:
                reviews.append(rv_to_dict(review))
                stage1_count += 1

        for review in extra_reviews_map.get(raw_id, []):
            reviews.append(rv_to_dict(review))
            stage2_count += 1

    log.debug(
        "gather_reviews",
        company=card.name_normalized,
        source_ids=len(source_ids),
        stage1_reviews=stage1_count,
        stage2_reviews=stage2_count,
        total=len(reviews),
    )
    return reviews[: settings.max_reviews_per_company]


def canonical_to_orm(
    card: CanonicalCard,
    summary: str | None,
    risk_level: str,
    risk_reasons: list[str],
    *,
    week_start: date,
) -> CompanyClean:
    return CompanyClean(
        id=uuid.uuid4(),
        inn=card.inn,
        ogrn=card.ogrn,
        name_normalized=card.name_normalized,
        source_name_primary=card.source_name_primary,
        legal_name=card.legal_name,
        legal_verified=card.legal_verified,
        relevance_score=card.relevance_score,
        geo_verified=card.geo_verified,
        quality_flags=card.quality_flags,
        entity_type=card.entity_type,
        phones=card.phones,
        emails=card.emails,
        addresses=card.addresses,
        contacts_json=card.contacts_json,
        average_rating=card.average_rating,
        reviews_count=card.reviews_count,
        reviews_sample=card.reviews_sample,
        summary_review=summary,
        risk_level=risk_level,
        risk_reasons=risk_reasons,
        source_records=card.source_records,
        merged_sources=card.merged_sources,
        source_links=card.source_links,
        checks=card.checks,
        manual_review_required=card.manual_review_required,
        pipeline_week_start=week_start,
    )


async def _get_or_create_checkpoint(
    session: AsyncSession,
    *,
    source: str,
    week_start: date,
) -> CollectionCheckpoint:
    stmt = select(CollectionCheckpoint).where(
        CollectionCheckpoint.source == source,
        CollectionCheckpoint.week_start == week_start,
    )
    checkpoint = (await session.execute(stmt)).scalar_one_or_none()
    if checkpoint is not None:
        return checkpoint

    checkpoint = CollectionCheckpoint(
        id=uuid.uuid4(),
        source=source,
        week_start=week_start,
        status="pending",
        companies_count=0,
        reviews_count=0,
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint
