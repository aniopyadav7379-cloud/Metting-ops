"""Add meeting_digest.emailed_at for the weekly-email-digest cron — v3.24.0.

The new scheduled weekly digest (workers/weekly_digest_workers.py) needs an
idempotency marker keyed to the cached digest row so a cron restart / manual
re-run / two-worker race never emails the same org twice for the same week.
The row is already unique-ish on (organization_id, period, date); this column
records when it was emailed. NULL = never emailed (eligible to send).

Nullable, no backfill: pre-existing digest rows finished before v3.24.0 simply
leave the column NULL. Mirrors the 036 `generation_job_id` add on the same
table.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "040_meeting_digest_emailed_at"
down_revision = "039_widen_alembic_ver"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_digest",
        sa.Column("emailed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_digest", "emailed_at")
