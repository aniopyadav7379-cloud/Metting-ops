"""Time-limited Pro/paid comp expiry on User.

Adds ``users.tier_expires_at`` (nullable timestamptz). When set, the user's
``tier`` is a *bounded* grant: the session-watchdog cron
(``services.session_watchdog.revert_expired_comps``) reverts ``tier`` to
'free' and clears this column once ``tier_expires_at < now()``. This lets an
admin comp an invited cohort a "free month" of Pro (``scripts/grant_pro.py``)
with no Stripe/card, and guarantees the comp can't silently become permanent.
NULL means a permanent tier — a real Stripe subscription clears the column
(``api.stripe_webhook``) so a paying customer, or a comped user who later
subscribes, is never auto-reverted.

Additive + safe: new nullable column; existing rows (NULL = permanent) and
every current tier path are unaffected.

Revision ID: 051_user_tier_expires_at
Revises: 050_upload_client_modified_at
"""
from alembic import op
import sqlalchemy as sa

revision = "051_user_tier_expires_at"
down_revision = "050_upload_client_modified_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tier_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "tier_expires_at")
