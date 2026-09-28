"""Track whether a beta invite code has already been emailed.

Adds ``beta_invite_codes.emailed_at`` (nullable timestamptz) so the launch
console can safely skip already-sent codes and keep invite distribution
idempotent across retries. The operator can dry-run repeatedly without
mutating the row; only a successful Postmark send stamps the column.

Additive + safe: new nullable column; existing rows remain untouched.

Revision ID: 052_beta_invite_codes_emailed_at
Revises: 051_user_tier_expires_at
"""
from alembic import op
import sqlalchemy as sa


revision = "052_beta_invite_codes_emailed_at"
down_revision = "051_user_tier_expires_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "beta_invite_codes",
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("beta_invite_codes", "emailed_at")
