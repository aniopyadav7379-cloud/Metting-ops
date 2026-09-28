"""tts_jobs table for async TTS rendering

Revision ID: 009_tts_jobs
Revises: 008_quota_columns
Create Date: 2026-05-06 00:01:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "009_tts_jobs"
down_revision = "008_quota_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tts_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("voice", sa.String(length=80), nullable=True),
        sa.Column("host_voice", sa.String(length=80), nullable=True),
        sa.Column("analyst_voice", sa.String(length=80), nullable=True),
        sa.Column("format", sa.String(length=8), nullable=False, server_default="mp3"),
        sa.Column("stage", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("job_started_at", sa.DateTime(), nullable=True),
        sa.Column("job_completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_tts_jobs_org_job_id", "tts_jobs", ["organization_id", "job_id"], unique=True)
    op.create_index("ix_tts_jobs_stage", "tts_jobs", ["stage"], unique=False)
    op.create_index("ix_tts_jobs_session_id", "tts_jobs", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tts_jobs_session_id", table_name="tts_jobs")
    op.drop_index("ix_tts_jobs_stage", table_name="tts_jobs")
    op.drop_index("ix_tts_jobs_org_job_id", table_name="tts_jobs")
    op.drop_table("tts_jobs")
