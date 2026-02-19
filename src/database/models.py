import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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

    confidence_score: Mapped[int | None] = mapped_column(Integer)
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False)

    enriched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    raw: Mapped["CompanyRaw"] = relationship(back_populates="enriched")


class CompanyClean(Base):
    """Финальная таблица подрядчиков."""

    __tablename__ = "companies_omsk_clean"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Identification
    inn: Mapped[str | None] = mapped_column(String(12), index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15))
    name_normalized: Mapped[str] = mapped_column(Text, index=True)
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

    # Registry checks
    checks: Mapped[dict | None] = mapped_column(JSONB)

    # Review flags
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
