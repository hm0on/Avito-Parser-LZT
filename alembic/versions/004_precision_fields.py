"""precision-first metadata fields

Revision ID: 004
Revises: 003
Create Date: 2026-03-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # companies_enriched
    op.add_column(
        "companies_enriched",
        sa.Column("legal_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("companies_enriched", sa.Column("legal_match_method", sa.String(length=50), nullable=True))
    op.add_column("companies_enriched", sa.Column("legal_match_score", sa.Float(), nullable=True))
    op.add_column("companies_enriched", sa.Column("relevance_score", sa.Float(), nullable=True))
    op.add_column("companies_enriched", sa.Column("relevance_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    # companies_omsk_clean
    op.add_column("companies_omsk_clean", sa.Column("source_name_primary", sa.Text(), nullable=True))
    op.add_column("companies_omsk_clean", sa.Column("legal_name", sa.Text(), nullable=True))
    op.add_column(
        "companies_omsk_clean",
        sa.Column("legal_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("companies_omsk_clean", sa.Column("relevance_score", sa.Float(), nullable=True))
    op.add_column(
        "companies_omsk_clean",
        sa.Column("geo_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("companies_omsk_clean", sa.Column("quality_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_index(
        "ix_companies_clean_legal_verified",
        "companies_omsk_clean",
        ["legal_verified"],
    )


def downgrade() -> None:
    op.drop_index("ix_companies_clean_legal_verified", table_name="companies_omsk_clean")

    op.drop_column("companies_omsk_clean", "quality_flags")
    op.drop_column("companies_omsk_clean", "geo_verified")
    op.drop_column("companies_omsk_clean", "relevance_score")
    op.drop_column("companies_omsk_clean", "legal_verified")
    op.drop_column("companies_omsk_clean", "legal_name")
    op.drop_column("companies_omsk_clean", "source_name_primary")

    op.drop_column("companies_enriched", "relevance_details")
    op.drop_column("companies_enriched", "relevance_score")
    op.drop_column("companies_enriched", "legal_match_score")
    op.drop_column("companies_enriched", "legal_match_method")
    op.drop_column("companies_enriched", "legal_verified")
