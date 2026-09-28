"""Add important_moments: timestamp-grounded moments (decision, action item,
key explanation, question, topic transition, disagreement, conclusion, key
lecture concept) for the meeting/lecture timeline view (Phase 3 module J).

Revision ID: 061_important_moments
Revises: 060_lecture_details
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa

revision = "061_important_moments"
down_revision = "060_lecture_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "important_moments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("moment_type", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        # Grounded server-side by matching `quote` against a real
        # Transcription row for this session — never trusted verbatim from
        # the LLM. NULL when no matching segment was found (never faked).
        sa.Column("timestamp", sa.Float(), nullable=True),
        sa.Column("speaker", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_important_moments_moment_type", "important_moments", ["moment_type"])


def downgrade() -> None:
    op.drop_index("ix_important_moments_moment_type", table_name="important_moments")
    op.drop_table("important_moments")
