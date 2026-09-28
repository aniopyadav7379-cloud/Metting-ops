"""Per-room retention opt-in flag.

Adds ``conference_rooms.retention_enabled`` (default false) so a room's
``default_retention_days`` (which carries a server_default of 90) no longer
creates an IMPLICIT purge policy. The scheduled compliance purge
(``services/data_retention.py``) now honors a room's retention horizon ONLY
when this flag is explicitly set; otherwise a room-attached session falls back
to the org-level policy (or no purge). Closes the "implicit 90-day delete"
landmine that would fire the moment an operator turned retention on.

Additive + safe: new boolean column, server_default false, so every existing
room is opted OUT (current effective behavior, since retention is off by
default) until an admin explicitly enables it.

Revision ID: 049_room_retention_opt_in
Revises: 048_analytics_and_preview
"""
from alembic import op
import sqlalchemy as sa

revision = "049_room_retention_opt_in"
down_revision = "048_analytics_and_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conference_rooms",
        sa.Column(
            "retention_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("conference_rooms", "retention_enabled")
