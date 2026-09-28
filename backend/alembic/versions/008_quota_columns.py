"""quota columns on organizations

Revision ID: 008_quota_columns
Revises: 007_uploads
Create Date: 2026-05-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "008_quota_columns"
down_revision = "007_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per-org quota overrides. NULL means "use the tier default from
    # services.quotas.TIER_DEFAULTS". Existing orgs are left at NULL so the
    # tier defaults apply going forward without retroactively shrinking
    # limits anyone is currently using.
    op.add_column(
        "organizations",
        sa.Column("max_file_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("max_concurrent_uploads", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "max_concurrent_uploads")
    op.drop_column("organizations", "max_file_bytes")
