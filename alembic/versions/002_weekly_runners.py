"""weekly runners and checkpoints

Revision ID: 002
Revises: 001
Create Date: 2026-02-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies_raw_omsk", sa.Column("collection_week_start", sa.Date(), nullable=True))
    op.create_index(
        "ix_companies_raw_collection_week_start",
        "companies_raw_omsk",
        ["collection_week_start"],
    )

    op.add_column("companies_enriched", sa.Column("pipeline_week_start", sa.Date(), nullable=True))
    op.create_index(
        "ix_companies_enriched_pipeline_week_start",
        "companies_enriched",
        ["pipeline_week_start"],
    )

    op.add_column("companies_omsk_clean", sa.Column("pipeline_week_start", sa.Date(), nullable=True))
    op.create_index(
        "ix_companies_clean_pipeline_week_start",
        "companies_omsk_clean",
        ["pipeline_week_start"],
    )

    op.create_table(
        "collection_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("companies_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reviews_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("details_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "week_start", name="uq_collection_checkpoints_source_week"),
    )
    op.create_index("ix_collection_checkpoints_source", "collection_checkpoints", ["source"])
    op.create_index("ix_collection_checkpoints_week_start", "collection_checkpoints", ["week_start"])


def downgrade() -> None:
    op.drop_index("ix_collection_checkpoints_week_start", table_name="collection_checkpoints")
    op.drop_index("ix_collection_checkpoints_source", table_name="collection_checkpoints")
    op.drop_table("collection_checkpoints")

    op.drop_index("ix_companies_clean_pipeline_week_start", table_name="companies_omsk_clean")
    op.drop_column("companies_omsk_clean", "pipeline_week_start")

    op.drop_index("ix_companies_enriched_pipeline_week_start", table_name="companies_enriched")
    op.drop_column("companies_enriched", "pipeline_week_start")

    op.drop_index("ix_companies_raw_collection_week_start", table_name="companies_raw_omsk")
    op.drop_column("companies_raw_omsk", "collection_week_start")
