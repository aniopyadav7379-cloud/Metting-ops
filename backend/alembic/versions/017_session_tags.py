"""recording_sessions.tags column

Revision ID: 017_session_tags
Revises: 016_session_participants
Create Date: 2026-05-17 00:00:00.000000

Adds a Postgres-native TEXT[] `tags` column to `recording_sessions` for
per-session free-form tagging. Array (not JSONB) so we get the `@>`
containment operator for cheap multi-tag AND filters from the list view.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "017_session_tags"
down_revision = "016_session_participants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    # GIN index for fast @> containment lookups from the list filter.
    op.create_index(
        "ix_recording_sessions_tags_gin",
        "recording_sessions",
        ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_recording_sessions_tags_gin", table_name="recording_sessions")
    op.drop_column("recording_sessions", "tags")
