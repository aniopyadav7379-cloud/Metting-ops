"""recording session mode

Revision ID: 020_session_mode
Revises: 017_session_tags
Create Date: 2026-05-18 00:00:00.000000

Adds an explicit mode column so browser always-on sessions can live beside
upload and single live-recording sessions without overloading status/source.
"""
from alembic import op
import sqlalchemy as sa


revision = "020_session_mode"
down_revision = "017_session_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column(
            "mode",
            sa.String(length=20),
            nullable=False,
            server_default="upload",
        ),
    )
    op.create_check_constraint(
        "ck_recording_sessions_mode",
        "recording_sessions",
        "mode IN ('upload', 'live', 'always_on')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_recording_sessions_mode",
        "recording_sessions",
        type_="check",
    )
    op.drop_column("recording_sessions", "mode")
