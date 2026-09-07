"""raw api, manual merge persistence, and on-demand summaries

Revision ID: 005
Revises: 004
Create Date: 2026-03-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("companies_omsk_clean", sa.Column("identity_key", sa.Text(), nullable=True))
    op.add_column("companies_omsk_clean", sa.Column("merge_group_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "companies_omsk_clean",
        sa.Column(
            "similar_company_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.create_index("ix_companies_clean_merge_group_id", "companies_omsk_clean", ["merge_group_id"])
    op.create_unique_constraint("uq_companies_clean_identity_key", "companies_omsk_clean", ["identity_key"])

    op.create_table(
        "company_merge_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("master_company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("primary_identity_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("master_company_id", name="uq_company_merge_groups_master_company_id"),
    )

    op.create_table(
        "company_merge_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["company_merge_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_company_merge_members_identity_key"),
        sa.UniqueConstraint("group_id", "identity_key", name="uq_merge_members_group_identity"),
    )
    op.create_index("ix_company_merge_members_group_id", "company_merge_members", ["group_id"])

    op.create_table(
        "company_summary_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("summary_review", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_company_summary_overrides_identity_key"),
    )


def downgrade() -> None:
    op.drop_table("company_summary_overrides")
    op.drop_index("ix_company_merge_members_group_id", table_name="company_merge_members")
    op.drop_table("company_merge_members")
    op.drop_table("company_merge_groups")
    op.drop_constraint("uq_companies_clean_identity_key", "companies_omsk_clean", type_="unique")
    op.drop_index("ix_companies_clean_merge_group_id", table_name="companies_omsk_clean")
    op.drop_column("companies_omsk_clean", "similar_company_ids")
    op.drop_column("companies_omsk_clean", "merge_group_id")
    op.drop_column("companies_omsk_clean", "identity_key")
