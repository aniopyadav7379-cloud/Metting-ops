"""Add processing_job_id + generation_job_id columns for v3.18.3 background jobs.

Revision ID: 035_processing_job_id
Revises: 034_merge_integrations_stripe
Create Date: 2026-05-29

Codex audit Performance Finding S1: long-running handlers (always-on
finalize, digest generation, TTS render) now enqueue an arq job and
return 202 immediately. Each row that has an in-flight job stamps the
arq job_id here so:

  - The frontend can resume polling /api/jobs/<id> after a tab reload
    without re-enqueueing.
  - The worker can drift-check on entry (if processing_job_id no longer
    matches this worker's job_id, the row was reprocessed by a newer
    job and we skip the side effects).

Both columns are nullable strings — no backfill needed; old rows
finished before v3.18.3 simply leave the column NULL.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "036_processing_job_id"
down_revision = "035_invite_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("processing_job_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_recording_sessions_processing_job_id",
        "recording_sessions",
        ["processing_job_id"],
    )
    op.add_column(
        "meeting_digest",
        sa.Column("generation_job_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meeting_digest", "generation_job_id")
    op.drop_index(
        "ix_recording_sessions_processing_job_id",
        table_name="recording_sessions",
    )
    op.drop_column("recording_sessions", "processing_job_id")
