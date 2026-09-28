"""Add lecture_details: lecture-specific structured fields, one-to-one with
recording_sessions. Lectures are NOT a renamed meeting — a RecordingSession
tagged meeting_type="lecture" carries this satellite row for the fields a
meeting has no use for (course/instructor/concepts/definitions/study notes/
questions), keeping the shared transcript/summary/Qdrant/Ask-AI pipeline
untouched for both domains.

Revision ID: 060_lecture_details
Revises: 059_integration_pat_scope
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "060_lecture_details"
down_revision = "059_integration_pat_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    json_type = sa.JSON() if is_sqlite else postgresql.JSONB()

    op.create_table(
        "lecture_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("recording_sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("course", sa.String(length=300), nullable=True),
        sa.Column("subject", sa.String(length=300), nullable=True),
        sa.Column("instructor", sa.String(length=300), nullable=True),
        sa.Column("lecture_number", sa.Integer(), nullable=True),
        # Structured extraction output (concepts/definitions/examples/
        # questions/study_notes) — same "LLM-generated, schema-validated"
        # discipline as ai_insights.action_items/key_decisions, just a
        # lecture-shaped schema instead of a meeting-shaped one.
        sa.Column("concepts", json_type, nullable=True),
        sa.Column("definitions", json_type, nullable=True),
        sa.Column("examples", json_type, nullable=True),
        sa.Column("questions", json_type, nullable=True),
        sa.Column("study_notes", sa.Text(), nullable=True),
        sa.Column("extraction_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lecture_details")
