"""uploads

Revision ID: 007_uploads
Revises: 006_speakers
Create Date: 2026-05-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "007_uploads"
down_revision = "006_speakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upload_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=False, server_default="transcribe"),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("chunks_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("last_completed_stage", sa.String(length=30), nullable=True),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("job_started_at", sa.DateTime(), nullable=True),
        sa.Column("job_completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_jobs_org_upload_id", "upload_jobs", ["organization_id", "upload_id"], unique=True)
    op.create_index("ix_upload_jobs_stage", "upload_jobs", ["stage"], unique=False)
    op.create_index(op.f("ix_upload_jobs_organization_id"), "upload_jobs", ["organization_id"], unique=False)
    op.create_index(op.f("ix_upload_jobs_upload_id"), "upload_jobs", ["upload_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_upload_jobs_upload_id"), table_name="upload_jobs")
    op.drop_index(op.f("ix_upload_jobs_organization_id"), table_name="upload_jobs")
    op.drop_index("ix_upload_jobs_stage", table_name="upload_jobs")
    op.drop_index("ix_upload_jobs_org_upload_id", table_name="upload_jobs")
    op.drop_table("upload_jobs")
