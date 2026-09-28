"""Add tier column + backfill Aaron+Shafen to enterprise+admin.

Revision ID: 028_user_tier
Revises: 027_meeting_date_time
Create Date: 2026-05-22 10:00:00.000000

Adds `users.tier` (free / pro / enterprise, default free) so every
auth check has a concrete capability bucket to look up. The TIER_FEATURES
dict in `backend/auth/tier.py` is the source of truth for what each
tier unlocks; this column just records which bucket a user falls in.

Backfill targets the two production accounts that always need the
top tier:
  - aaron@magicunicorn.tech  (CTO / platform owner)
  - connect@shafenkhan.com   (co-founder)
Both also get is_superuser=true. tier and is_superuser are orthogonal
in normal use (a superuser flag is admin powers, tier is feature
gating), but the helper `get_user_tier()` treats is_superuser as an
override to enterprise so support staff can debug feature gates
without having to manually flip tier columns.

Index on tier supports the analytics queries that segment usage by
plan ("how many enterprise users hit /api/sessions this week"). Tiny
table so the index is cheap; no partial predicate.

Down: drop index + column. Rollback aid only, not a workflow — once
a column is populated in prod, dropping it loses data.
"""

from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa


revision = "028_user_tier"
down_revision = "027_meeting_date_time"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.028_user_tier")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "tier",
            sa.String(length=32),
            nullable=False,
            server_default="free",
        ),
    )

    # Backfill the two production accounts that always need enterprise +
    # admin. UPDATE is idempotent so re-running the migration after a
    # restore doesn't blow up.
    op.execute(
        "UPDATE users "
        "SET tier = 'enterprise', is_superuser = true "
        "WHERE email IN ('aaron@magicunicorn.tech', 'connect@shafenkhan.com')"
    )

    op.create_index(
        "ix_users_tier",
        "users",
        ["tier"],
    )


def downgrade() -> None:
    op.drop_index("ix_users_tier", table_name="users")
    op.drop_column("users", "tier")
