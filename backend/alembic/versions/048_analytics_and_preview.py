"""Denormalized list preview and analytics index.

Revision ID: 048_analytics_and_preview
Revises: 047_session_list_pagination
"""
from alembic import op
import sqlalchemy as sa

revision = "048_analytics_and_preview"
down_revision = "047_session_list_pagination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recording_sessions", sa.Column("summary_preview", sa.String(300), nullable=True))
    op.execute("""
        UPDATE recording_sessions
        SET summary_preview = LEFT(
            COALESCE(final_summary::jsonb ->> 'executive', summary, ''),
            300
        )
    """)
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_transcriptions_session_speaker",
            "transcriptions",
            ["session_id", "speaker"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_transcriptions_session_speaker",
            table_name="transcriptions",
            postgresql_concurrently=True,
        )
    op.drop_column("recording_sessions", "summary_preview")
