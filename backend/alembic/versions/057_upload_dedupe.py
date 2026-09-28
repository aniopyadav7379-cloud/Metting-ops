"""upload de-dupe: carry the audio SHA-256 on the upload job

Revision ID: 057_upload_dedupe
Revises: 056_brigade_sync_observability
Create Date: 2026-07-26

The single-file upload path never hashed anything, so re-uploading the same
file produced a second session and a second full STT + diarization +
summarization run. The bulk-import path already de-duped on SHA-256; this lets
the interactive path use the same mechanism.

The hash is computed once at finalize (where the assembled file first exists)
and stored here, because the pipeline that creates the RecordingSession runs in
a SEPARATE worker process and cannot receive it in memory.

Indexed because finalize looks up by it on every upload.
"""
from alembic import op
import sqlalchemy as sa

revision = "057_upload_dedupe"
down_revision = "056_brigade_sync_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "upload_jobs",
        sa.Column("audio_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_upload_jobs_audio_sha256", "upload_jobs", ["audio_sha256"], unique=False
    )
    # The session-side lookup filters on organization_id and then matches the
    # hash inside processing_metadata. Index the org side so the scan is bounded
    # per tenant rather than per table.
    op.create_index(
        "ix_recording_sessions_org_status",
        "recording_sessions",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_recording_sessions_org_status", table_name="recording_sessions")
    op.drop_index("ix_upload_jobs_audio_sha256", table_name="upload_jobs")
    op.drop_column("upload_jobs", "audio_sha256")
