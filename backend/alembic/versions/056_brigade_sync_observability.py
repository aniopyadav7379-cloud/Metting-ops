"""Persist Brigade graph projection status and retry diagnostics.

Revision ID: 056_brigade_sync_observability
Revises: 055_federation_summary_approval
"""

from alembic import op
import sqlalchemy as sa


revision = "056_brigade_sync_observability"
down_revision = "055_federation_summary_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recording_sessions", sa.Column("brigade_sync_status", sa.String(length=20), nullable=True))
    op.add_column("recording_sessions", sa.Column("brigade_sync_attempted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("recording_sessions", sa.Column("brigade_sync_error", sa.String(length=500), nullable=True))
    op.add_column("recording_sessions", sa.Column("brigade_sync_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_recording_sessions_brigade_sync_status", "recording_sessions", ["brigade_sync_status"])


def downgrade() -> None:
    op.drop_index("ix_recording_sessions_brigade_sync_status", table_name="recording_sessions")
    op.drop_column("recording_sessions", "brigade_sync_attempt_count")
    op.drop_column("recording_sessions", "brigade_sync_error")
    op.drop_column("recording_sessions", "brigade_sync_attempted_at")
    op.drop_column("recording_sessions", "brigade_sync_status")
