"""Editable meeting_date + meeting_time on recording_sessions.

Revision ID: 027_meeting_date_time
Revises: 026_qwen36_consolidation
Create Date: 2026-05-22 02:00:00.000000

Adds two columns the user can edit directly on a session, separate
from the system-tracked `started_at` / `created_at`. The list view
sorts by these (with started_at as fallback when null), and inline
editors on SessionDetails write to them.

Backfill is in-migration:
  meeting_date = COALESCE(DATE(started_at), DATE(created_at))
  meeting_time = TIME(started_at)  -- skip when started_at is null

Both columns are nullable so backfill can be partial. Postgres-only
production target; SQLite (test fixture) takes the same DDL with the
trivial type fallbacks DATE / TIME — SQLAlchemy compiles cleanly.

Down: drop both columns. Down is a rollback aid, not a workflow.

Foundation for the upcoming /import page that will backfill Aaron's
526-file audio archive at /Volumes/media/audio-from-notes-voicememos-
2026-05-20 — each file's meeting_date / meeting_time is parsed from
the `{notes|downloads}__YYYY-MM-DD_HHMMSS__title.{m4a|mp3}` shape and
written here, not into started_at (started_at stays the actual upload
ingest time so processing analytics aren't poisoned by historical
backfill).
"""

from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa


revision = "027_meeting_date_time"
down_revision = "026_qwen36_consolidation"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.027_meeting_date_time")


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("meeting_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "recording_sessions",
        sa.Column("meeting_time", sa.Time(), nullable=True),
    )

    # Backfill from started_at first, then created_at. Postgres and
    # SQLite both accept CAST(... AS DATE) / CAST(... AS TIME) on
    # timestamp columns, so this stays portable for the test fixture.
    op.execute(
        "UPDATE recording_sessions "
        "SET meeting_date = COALESCE("
        "    CAST(started_at AS DATE), "
        "    CAST(created_at AS DATE)"
        ") "
        "WHERE meeting_date IS NULL"
    )
    op.execute(
        "UPDATE recording_sessions "
        "SET meeting_time = CAST(started_at AS TIME) "
        "WHERE meeting_time IS NULL AND started_at IS NOT NULL"
    )

    # Index for sort-by-meeting-date list queries. Partial index on
    # NOT NULL keeps it small until backfill finishes against the
    # 526-file audio archive import.
    op.create_index(
        "ix_recording_sessions_meeting_date",
        "recording_sessions",
        ["meeting_date"],
        postgresql_where=sa.text("meeting_date IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recording_sessions_meeting_date",
        table_name="recording_sessions",
    )
    op.drop_column("recording_sessions", "meeting_time")
    op.drop_column("recording_sessions", "meeting_date")
