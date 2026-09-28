"""per-upload transcription_options on upload_jobs

Revision ID: 011_upload_transcription_options
Revises: 010_project_id_to_text
Create Date: 2026-05-08 00:00:00.000000

Lets a single upload pick its own STT provider, diarization mode, voice
fingerprint enrichment, and summary template — overriding the per-org
defaults from org_provider_settings. NULL = use org defaults end-to-end.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "011_upload_transcription_options"
down_revision = "010_project_id_to_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "upload_jobs",
        sa.Column("transcription_options", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_jobs", "transcription_options")
