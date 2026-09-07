"""CSV / JSON export endpoint."""

from __future__ import annotations

import csv
import io
import json
from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.query_filters import apply_source_filter
from src.database.models import CompanyClean
from src.database.session import get_session

router = APIRouter(prefix="/exports", tags=["exports"])

_CSV_FIELDS = [
    "id",
    "name_normalized",
    "source_name_primary",
    "legal_name",
    "legal_verified",
    "relevance_score",
    "geo_verified",
    "quality_flags",
    "inn",
    "ogrn",
    "entity_type",
    "phones",
    "emails",
    "addresses",
    "average_rating",
    "reviews_count",
    "risk_level",
    "risk_reasons",
    "merged_sources",
    "source_links",
    "similar_company_ids",
    "pipeline_week_start",
    "manual_review_required",
    "last_updated",
    "created_at",
]


@router.get("")
async def export_companies(
    format: str = Query("csv", pattern="^(csv|json)$"),
    risk: str | None = Query(None, pattern="^(green|yellow|red)$"),
    min_rating: float | None = Query(None, ge=0.0, le=5.0),
    source: str | None = Query(None, description="Фильтр по источнику компании: avito, 2gis, yandex"),
    manual_review: bool | None = Query(None),
    legal_verified: bool | None = Query(
        True,
        description="Только юридически верифицированные компании (default=true)",
    ),
    min_relevance_score: float | None = Query(
        None,
        ge=0.0,
        le=1.0,
    ),
    geo_verified: bool | None = Query(None),
    week_start: date | None = Query(
        None,
        description="Дата начала недели (понедельник) в формате YYYY-MM-DD",
    ),
    latest_week: bool = Query(
        True,
        description="Вернуть только записи последней доступной недели",
    ),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    stmt = select(CompanyClean)

    if risk:
        stmt = stmt.where(CompanyClean.risk_level == risk)
    if min_rating is not None:
        stmt = stmt.where(CompanyClean.average_rating >= min_rating)
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

    stmt = stmt.order_by(CompanyClean.last_updated.desc())
    rows = (await session.execute(stmt)).scalars().all()

    if format == "csv":
        return _stream_csv(rows)
    return _stream_json(rows)


def _row_to_dict(row: CompanyClean) -> dict:
    return {
        "id": str(row.id),
        "name_normalized": row.name_normalized,
        "source_name_primary": row.source_name_primary,
        "legal_name": row.legal_name,
        "legal_verified": row.legal_verified,
        "relevance_score": row.relevance_score,
        "geo_verified": row.geo_verified,
        "quality_flags": json.dumps(row.quality_flags or {}, ensure_ascii=False),
        "inn": row.inn,
        "ogrn": row.ogrn,
        "entity_type": row.entity_type,
        "phones": json.dumps(row.phones or [], ensure_ascii=False),
        "emails": json.dumps(row.emails or [], ensure_ascii=False),
        "addresses": json.dumps(row.addresses or [], ensure_ascii=False),
        "average_rating": row.average_rating,
        "reviews_count": row.reviews_count,
        "risk_level": row.risk_level,
        "risk_reasons": json.dumps(row.risk_reasons or [], ensure_ascii=False),
        "merged_sources": json.dumps(row.merged_sources or [], ensure_ascii=False),
        "source_links": json.dumps(row.source_links or [], ensure_ascii=False),
        "similar_company_ids": json.dumps(row.similar_company_ids or [], ensure_ascii=False),
        "pipeline_week_start": row.pipeline_week_start.isoformat() if row.pipeline_week_start else "",
        "manual_review_required": row.manual_review_required,
        "last_updated": row.last_updated.isoformat() if row.last_updated else "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _stream_csv(rows: list[CompanyClean]) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(_row_to_dict(row))
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=companies.csv"},
    )


def _stream_json(rows: list[CompanyClean]) -> StreamingResponse:
    data = json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False, indent=2)

    return StreamingResponse(
        iter([data]),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=companies.json"},
    )
