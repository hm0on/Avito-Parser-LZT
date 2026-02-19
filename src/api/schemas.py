"""Pydantic v2 API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReviewSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    text: str | None = None
    rating: float | None = None
    author: str | None = None
    review_date: datetime | None = None
    source_link: str | None = None


class ChecksSchema(BaseModel):
    """Registry check results — arbitrary keyed dict."""

    model_config = ConfigDict(extra="allow")


class CompanyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name_normalized: str
    inn: str | None = None
    entity_type: str | None = None
    phones: list[str] | None = None
    average_rating: float | None = None
    reviews_count: int | None = None
    risk_level: str | None = None
    merged_sources: list[str] | None = None
    manual_review_required: bool = False
    last_updated: datetime


class CompanyDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    inn: str | None = None
    ogrn: str | None = None
    name_normalized: str
    entity_type: str | None = None

    phones: list[str] | None = None
    emails: list[str] | None = None
    addresses: list[Any] | None = None
    contacts_json: dict | None = None

    average_rating: float | None = None
    reviews_count: int | None = None
    reviews_sample: list[dict] | None = None

    summary_review: str | None = None

    risk_level: str | None = None
    risk_reasons: list[str] | None = None

    source_records: list[str] | None = None
    merged_sources: list[str] | None = None
    checks: dict | None = None

    manual_review_required: bool = False
    last_updated: datetime
    created_at: datetime


class ManualReviewPayload(BaseModel):
    reviewer_notes: str = Field(..., min_length=1)
    resolved_risk_level: str = Field(pattern="^(green|yellow|red)$")
    clear_manual_flag: bool = True


class PaginatedCompanies(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[CompanyListItem]
