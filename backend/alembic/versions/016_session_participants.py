"""recording_sessions.participants column

Revision ID: 016_session_participants
Revises: 015_speaker_profile_contacts
Create Date: 2026-05-17 00:00:00.000000

Adds a JSONB `participants` column to `recording_sessions` for per-session
attendee tracking. Shape: [{"name": str, "email": str|null, "role": str|null}].

Stored alongside the session (not a side table) because the list is small,
always loaded with the session, and never queried independently. Switch to
a side table if attendance reporting requires per-attendee aggregation.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "016_session_participants"
down_revision = "015_speaker_profile_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column(
            "participants",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("recording_sessions", "participants")
