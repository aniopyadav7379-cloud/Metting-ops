"""title_user_set flag on recording_sessions

Revision ID: 013_session_title_user_set
Revises: 012_cascade_session_children
Create Date: 2026-05-15 00:00:00.000000

Track when the user has explicitly set a session title so the
auto-summary step (which already extracts a title from the meeting
transcript) doesnt overwrite a manual rename on the next reprocess.
"""

from alembic import op
import sqlalchemy as sa


revision = "013_session_title_user_set"
down_revision = "012_cascade_session_children"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column(
            "title_user_set",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("recording_sessions", "title_user_set")
