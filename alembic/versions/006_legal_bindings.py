"""persistent legal bindings for source cards

Revision ID: 006
Revises: 005
Create Date: 2026-03-09 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_legal_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("source_name_snapshot", sa.Text(), nullable=True),
        sa.Column("legal_name", sa.Text(), nullable=True),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("ogrn", sa.String(length=15), nullable=True),
        sa.Column("binding_strength", sa.String(length=32), nullable=True),
        sa.Column("binding_source", sa.String(length=64), nullable=True),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_company_legal_bindings_identity_key"),
    )
    op.create_index("ix_company_legal_bindings_identity_key", "company_legal_bindings", ["identity_key"])


def downgrade() -> None:
    op.drop_index("ix_company_legal_bindings_identity_key", table_name="company_legal_bindings")
    op.drop_table("company_legal_bindings")
