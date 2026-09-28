"""Bulk audio import tables — Phase 1 of /import.

Revision ID: 029_bulk_import
Revises: 028_user_tier
Create Date: 2026-05-22 14:00:00.000000

Adds two tables to back the new /import page:

  bulk_import_jobs   — one row per drag-and-drop session. Tracks total /
                       succeeded / failed / skipped counts plus the
                       cancelled_at + started_at + finished_at lifecycle
                       timestamps that drive the progress UI.

  bulk_import_files  — one row per uploaded audio file inside a job.
                       Carries the SHA-256 dedup key, parsed_* fields
                       from the shared filename parser, session_id_when_
                       created (NULL when skipped duplicate / failed),
                       and per-row status the worker pool transitions.

UUID PKs on both so file_id / job_id appear cleanly in URLs without
leaking row counts. FK cascade from job → files so dropping a job tears
down its file rows; session_id_when_created stays nullable + non-FK
(referencing recording_sessions.id is fine but a SET NULL there is
heavier than the audit trail benefit, since the file row's whole point
is to survive even if the session it created is later deleted).

Indexes:
  - (user_id, status) on jobs — drives the "my jobs" filter
  - (job_id, status) on files — drives the per-job dashboard
  - (file_sha256)   on files — drives the dedup lookup at submit time

Down is reversible — drops indexes then tables. Postgres-only
production target; SQLite test fixture takes the same DDL with UUID →
CHAR(36) handled by conftest.py's compile rule.

Foundation for ingesting Aaron's 526-file Voice Memos + Mac Notes
archive at /Volumes/media/audio-from-notes-voicememos-2026-05-20. Per
the bulk-audio-import-design.md doc, this is B-import.1; speaker auto-
link (B-import.3) and Arq+Redis migration (B-import.4) are subsequent
phases.
"""

from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "029_bulk_import"
down_revision = "028_user_tier"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.029_bulk_import")


def upgrade() -> None:
    op.create_table(
        "bulk_import_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "total_files",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "succeeded",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "failed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "skipped",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_bulk_import_jobs_user_status",
        "bulk_import_jobs",
        ["user_id", "status"],
    )

    op.create_table(
        "bulk_import_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bulk_import_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("parsed_title", sa.Text(), nullable=True),
        sa.Column("parsed_date", sa.Date(), nullable=True),
        sa.Column("parsed_time", sa.Time(), nullable=True),
        sa.Column("parsed_source", sa.String(length=32), nullable=True),
        sa.Column("parsed_confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        # No FK on session_id_when_created on purpose — see module docstring.
        # The file row is the historical import record; it must outlive the
        # session it created if that session is later deleted by the user.
        sa.Column(
            "session_id_when_created",
            UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("bytes_total", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_bulk_import_files_job_status",
        "bulk_import_files",
        ["job_id", "status"],
    )
    op.create_index(
        "ix_bulk_import_files_sha256",
        "bulk_import_files",
        ["file_sha256"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bulk_import_files_sha256", table_name="bulk_import_files"
    )
    op.drop_index(
        "ix_bulk_import_files_job_status", table_name="bulk_import_files"
    )
    op.drop_table("bulk_import_files")
    op.drop_index(
        "ix_bulk_import_jobs_user_status", table_name="bulk_import_jobs"
    )
    op.drop_table("bulk_import_jobs")
