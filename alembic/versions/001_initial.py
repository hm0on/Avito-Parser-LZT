"""initial

Revision ID: 001
Revises:
Create Date: 2026-02-19 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # companies_raw_omsk
    op.create_table(
        "companies_raw_omsk",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("source_link", sa.Text(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        sa.Column("name_raw", sa.Text(), nullable=True),
        sa.Column("phones", postgresql.JSONB(), nullable=True),
        sa.Column("emails", postgresql.JSONB(), nullable=True),
        sa.Column("addresses", postgresql.JSONB(), nullable=True),
        sa.Column("contacts_json", postgresql.JSONB(), nullable=True),
        sa.Column("inn", sa.String(12), nullable=True),
        sa.Column("ogrn", sa.String(15), nullable=True),
        sa.Column("average_rating", sa.Float(), nullable=True),
        sa.Column("reviews_count", sa.Integer(), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("is_processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companies_raw_source", "companies_raw_omsk", ["source"])
    op.create_index("ix_companies_raw_inn", "companies_raw_omsk", ["inn"])
    op.create_index("ix_companies_raw_is_processed", "companies_raw_omsk", ["is_processed"])

    # reviews_raw_omsk
    op.create_table(
        "reviews_raw_omsk",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("company_raw_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("review_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_link", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_raw_id"],
            ["companies_raw_omsk.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviews_raw_company_id", "reviews_raw_omsk", ["company_raw_id"])

    # companies_enriched
    op.create_table(
        "companies_enriched",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inn", sa.String(12), nullable=True),
        sa.Column("ogrn", sa.String(15), nullable=True),
        sa.Column("name_normalized", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(20), nullable=True),
        sa.Column("phones_normalized", postgresql.JSONB(), nullable=True),
        sa.Column("addresses_parsed", postgresql.JSONB(), nullable=True),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column("confidence_score", sa.Integer(), nullable=True),
        sa.Column("manual_review_required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "enriched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["raw_id"], ["companies_raw_omsk.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_id"),
    )
    op.create_index("ix_companies_enriched_inn", "companies_enriched", ["inn"])

    # companies_omsk_clean
    op.create_table(
        "companies_omsk_clean",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inn", sa.String(12), nullable=True),
        sa.Column("ogrn", sa.String(15), nullable=True),
        sa.Column("name_normalized", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(20), nullable=True),
        sa.Column("phones", postgresql.JSONB(), nullable=True),
        sa.Column("emails", postgresql.JSONB(), nullable=True),
        sa.Column("addresses", postgresql.JSONB(), nullable=True),
        sa.Column("contacts_json", postgresql.JSONB(), nullable=True),
        sa.Column("average_rating", sa.Float(), nullable=True),
        sa.Column("reviews_count", sa.Integer(), nullable=True),
        sa.Column("reviews_sample", postgresql.JSONB(), nullable=True),
        sa.Column("summary_review", sa.Text(), nullable=True),
        sa.Column("risk_level", sa.String(10), nullable=True),
        sa.Column("risk_reasons", postgresql.JSONB(), nullable=True),
        sa.Column("source_records", postgresql.JSONB(), nullable=True),
        sa.Column("merged_sources", postgresql.JSONB(), nullable=True),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column("manual_review_required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companies_clean_inn", "companies_omsk_clean", ["inn"])
    op.create_index("ix_companies_clean_name", "companies_omsk_clean", ["name_normalized"])
    op.create_index("ix_companies_clean_risk", "companies_omsk_clean", ["risk_level"])


def downgrade() -> None:
    op.drop_table("companies_omsk_clean")
    op.drop_table("companies_enriched")
    op.drop_table("reviews_raw_omsk")
    op.drop_table("companies_raw_omsk")
