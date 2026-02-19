"""CSV / JSON export endpoint."""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import CompanyClean
from src.database.session import get_session

router = APIRouter(prefix="/exports", tags=["exports"])

_CSV_FIELDS = [
    "id",
    "name_normalized",
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
    "manual_review_required",
    "last_updated",
    "created_at",
]


@router.get("")
async def export_companies(
    format: str = Query("csv", pattern="^(csv|json)$"),
    risk: str | None = Query(None, pattern="^(green|yellow|red)$"),
    min_rating: float | None = Query(None, ge=0.0, le=5.0),
    manual_review: bool | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    stmt = select(CompanyClean)

    if risk:
        stmt = stmt.where(CompanyClean.risk_level == risk)
    if min_rating is not None:
        stmt = stmt.where(CompanyClean.average_rating >= min_rating)
    if manual_review is not None:
        stmt = stmt.where(CompanyClean.manual_review_required == manual_review)

    stmt = stmt.order_by(CompanyClean.last_updated.desc())
    rows = (await session.execute(stmt)).scalars().all()

    if format == "csv":
        return _stream_csv(rows)
    return _stream_json(rows)


def _row_to_dict(row: CompanyClean) -> dict:
    return {
        "id": str(row.id),
        "name_normalized": row.name_normalized,
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
