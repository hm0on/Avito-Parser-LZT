import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CompanyRaw(Base):
    """Сырые записи, собранные с площадок."""

    __tablename__ = "companies_raw_omsk"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50))  # avito/2gis/yandex/vk/flamp/otzovik
    source_id: Mapped[str | None] = mapped_column(String(255))
    source_link: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)

    name_raw: Mapped[str | None] = mapped_column(Text)
    phones: Mapped[list | None] = mapped_column(JSONB)
    emails: Mapped[list | None] = mapped_column(JSONB)
    addresses: Mapped[list | None] = mapped_column(JSONB)
    contacts_json: Mapped[dict | None] = mapped_column(JSONB)

    inn: Mapped[str | None] = mapped_column(String(12))
    ogrn: Mapped[str | None] = mapped_column(String(15))

    average_rating: Mapped[float | None] = mapped_column(Float)
    reviews_count: Mapped[int | None] = mapped_column(Integer)
    collection_week_start: Mapped[date | None] = mapped_column(Date, index=True)

    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False)

    reviews: Mapped[list["ReviewRaw"]] = relationship(back_populates="company_raw")
    enriched: Mapped["CompanyEnriched | None"] = relationship(back_populates="raw")


class ReviewRaw(Base):
    """Сырые отзывы."""

    __tablename__ = "reviews_raw_omsk"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_raw_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies_raw_omsk.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String(50))

    text: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[float | None] = mapped_column(Float)
    author: Mapped[str | None] = mapped_column(String(255))
    review_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_link: Mapped[str | None] = mapped_column(Text)

    company_raw: Mapped["CompanyRaw"] = relationship(back_populates="reviews")


class CompanyEnriched(Base):
    """Результаты обогащения (промежуточная таблица)."""

    __tablename__ = "companies_enriched"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies_raw_omsk.id", ondelete="CASCADE"), unique=True
    )

    inn: Mapped[str | None] = mapped_column(String(12))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    name_normalized: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(20))  # ЮЛ/ИП/ФЛ/unknown

    phones_normalized: Mapped[list | None] = mapped_column(JSONB)
    addresses_parsed: Mapped[dict | None] = mapped_column(JSONB)

    checks: Mapped[dict | None] = mapped_column(JSONB)  # результаты реестровых проверок

    legal_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    legal_match_method: Mapped[str | None] = mapped_column(String(50))
    legal_match_score: Mapped[float | None] = mapped_column(Float)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    relevance_details: Mapped[dict | None] = mapped_column(JSONB)

    confidence_score: Mapped[int | None] = mapped_column(Integer)
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    pipeline_week_start: Mapped[date | None] = mapped_column(Date, index=True)

    enriched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    raw: Mapped["CompanyRaw"] = relationship(back_populates="enriched")


class CompanyClean(Base):
    """Финальная таблица подрядчиков."""

    __tablename__ = "companies_omsk_clean"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identity_key: Mapped[str | None] = mapped_column(Text, unique=True)
    merge_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)

    # Identification
    inn: Mapped[str | None] = mapped_column(String(12), index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15))
    name_normalized: Mapped[str] = mapped_column(Text, index=True)
    source_name_primary: Mapped[str | None] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text)
    legal_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    geo_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_flags: Mapped[dict | None] = mapped_column(JSONB)
    entity_type: Mapped[str | None] = mapped_column(String(20))  # ЮЛ/ИП/ФЛ/unknown

    # Contacts
    phones: Mapped[list | None] = mapped_column(JSONB)
    emails: Mapped[list | None] = mapped_column(JSONB)
    addresses: Mapped[list | None] = mapped_column(JSONB)
    contacts_json: Mapped[dict | None] = mapped_column(JSONB)

    # Ratings & reviews
    average_rating: Mapped[float | None] = mapped_column(Float)
    reviews_count: Mapped[int | None] = mapped_column(Integer)
    reviews_sample: Mapped[list | None] = mapped_column(JSONB)  # до MAX_REVIEWS отзывов

    # AI output
    summary_review: Mapped[str | None] = mapped_column(Text)

    # Risk
    risk_level: Mapped[str | None] = mapped_column(String(10))  # green/yellow/red
    risk_reasons: Mapped[list | None] = mapped_column(JSONB)  # max 5 пунктов

    # Deduplication / merge info
    source_records: Mapped[list | None] = mapped_column(JSONB)  # список raw_id
    merged_sources: Mapped[list | None] = mapped_column(JSONB)  # avito/2gis/...
    source_links: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    similar_company_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    # Registry checks
    checks: Mapped[dict | None] = mapped_column(JSONB)

    # Review flags
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    pipeline_week_start: Mapped[date | None] = mapped_column(Date, index=True)

    # Timestamps
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CompanyMergeGroup(Base):
    """Persistent manual merge group that survives reruns."""

    __tablename__ = "company_merge_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    master_company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)
    primary_identity_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanyMergeMember(Base):
    """Members of a manual merge group identified by stable identity_key."""

    __tablename__ = "company_merge_members"
    __table_args__ = (
        UniqueConstraint("group_id", "identity_key", name="uq_merge_members_group_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("company_merge_groups.id", ondelete="CASCADE"), index=True
    )
    identity_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CompanySummaryOverride(Base):
    """Persistent manual summary keyed by stable identity."""

    __tablename__ = "company_summary_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identity_key: Mapped[str] = mapped_column(Text, unique=True)
    summary_review: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanyLegalBinding(Base):
    """Persistent source-card -> legal entity binding based on strong evidence."""

    __tablename__ = "company_legal_bindings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identity_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    source_name_snapshot: Mapped[str | None] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text)
    inn: Mapped[str | None] = mapped_column(String(12))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    binding_strength: Mapped[str | None] = mapped_column(String(32))
    binding_source: Mapped[str | None] = mapped_column(String(64))
    evidence_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CollectionCheckpoint(Base):
    """Weekly per-source completion state, including enrichment."""

    __tablename__ = "collection_checkpoints"
    __table_args__ = (
        UniqueConstraint("source", "week_start", name="uq_collection_checkpoints_source_week"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50), index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    companies_count: Mapped[int] = mapped_column(Integer, default=0)
    reviews_count: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
