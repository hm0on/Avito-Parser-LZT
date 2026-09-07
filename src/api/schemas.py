"""Pydantic v2 API schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    source_name_primary: str | None = None
    legal_name: str | None = None
    legal_verified: bool = False
    relevance_score: float | None = None
    geo_verified: bool = False
    inn: str | None = None
    entity_type: str | None = None
    phones: list[str] | None = None
    addresses: list[Any] | None = None
    average_rating: float | None = None
    reviews_count: int | None = None
    risk_level: str | None = None
    merged_sources: list[str] | None = None
    source_links: list[dict] | None = None
    similar_company_ids: list[uuid.UUID] | None = None
    pipeline_week_start: date | None = None
    manual_review_required: bool = False
    last_updated: datetime


class CompanyDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    inn: str | None = None
    ogrn: str | None = None
    name_normalized: str
    source_name_primary: str | None = None
    legal_name: str | None = None
    legal_verified: bool = False
    relevance_score: float | None = None
    geo_verified: bool = False
    quality_flags: dict | None = None
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
    source_links: list[dict] | None = None
    similar_company_ids: list[uuid.UUID] | None = None
    checks: dict | None = None
    pipeline_week_start: date | None = None

    manual_review_required: bool = False
    last_updated: datetime
    created_at: datetime


class RawCompanyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    source_id: str | None = None
    source_link: str | None = None
    name_raw: str | None = None
    phones: list[Any] | None = None
    emails: list[Any] | None = None
    addresses: list[Any] | None = None
    contacts_json: dict | None = None
    inn: str | None = None
    ogrn: str | None = None
    average_rating: float | None = None
    reviews_count: int | None = None
    collection_week_start: date | None = None
    collected_at: datetime
    is_processed: bool = False


class RawCompanyDetail(RawCompanyListItem):
    raw_payload: dict | None = None


class RawReviewItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_raw_id: uuid.UUID
    source: str
    text: str | None = None
    rating: float | None = None
    author: str | None = None
    review_date: datetime | None = None
    source_link: str | None = None


class EnrichedCompanyListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    raw_id: uuid.UUID
    source: str
    source_id: str | None = None
    source_link: str | None = None
    name_raw: str | None = None
    name_normalized: str | None = None
    legal_verified: bool = False
    legal_match_method: str | None = None
    legal_match_score: float | None = None
    relevance_score: float | None = None
    confidence_score: int | None = None
    manual_review_required: bool = False
    pipeline_week_start: date | None = None
    enriched_at: datetime


class EnrichedCompanyDetail(EnrichedCompanyListItem):
    raw_payload: dict | None = None
    raw_phones: list[Any] | None = None
    raw_emails: list[Any] | None = None
    raw_addresses: list[Any] | None = None
    raw_contacts_json: dict | None = None
    raw_inn: str | None = None
    raw_ogrn: str | None = None
    raw_average_rating: float | None = None
    raw_reviews_count: int | None = None
    collection_week_start: date | None = None
    inn: str | None = None
    ogrn: str | None = None
    entity_type: str | None = None
    phones_normalized: list[Any] | None = None
    addresses_parsed: dict | list[Any] | None = None
    checks: dict | None = None
    relevance_details: dict | None = None


class ManualReviewPayload(BaseModel):
    reviewer_notes: str = Field(..., min_length=1)
    resolved_risk_level: str = Field(pattern="^(green|yellow|red)$")
    clear_manual_flag: bool = True


class SummarizeReviewsPayload(BaseModel):
    force: bool = False


class MergeCompaniesPayload(BaseModel):
    company_ids: list[uuid.UUID] = Field(..., min_length=2)
    primary_company_id: uuid.UUID | None = None


class AvitoPhoneLookupResponse(BaseModel):
    company_id: uuid.UUID
    avito_url: str
    ad_id: str
    phone: str
    provider: str = "spfa"
    provider_status: int
    raw_response: dict[str, Any] | None = None


class CompanyDeleteResponse(BaseModel):
    company_id: uuid.UUID
    deleted: bool = True
    deleted_from: str = "companies_omsk_clean"


class CompanyMergeResponse(BaseModel):
    master_company_id: uuid.UUID
    merged_company_ids: list[uuid.UUID]
    company: CompanyDetail


class PaginatedCompanies(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[CompanyListItem]


class PaginatedRawCompanies(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[RawCompanyListItem]


class PaginatedRawReviews(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[RawReviewItem]


class PaginatedEnrichedCompanies(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[EnrichedCompanyListItem]
