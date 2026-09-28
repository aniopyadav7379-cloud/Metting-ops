"""Thin session list pagination support.

Revision ID: 047_session_list_pagination
Revises: 046_beta_invite_codes
"""
from alembic import op
import sqlalchemy as sa

revision = "047_session_list_pagination"
down_revision = "046_beta_invite_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("speaker_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("""
        UPDATE recording_sessions
        SET speaker_count = CASE
            WHEN transcript_diarized IS NOT NULL
             AND jsonb_typeof(transcript_diarized::jsonb -> 'speakers') = 'array'
            THEN jsonb_array_length(transcript_diarized::jsonb -> 'speakers')
            ELSE 0
        END
    """)
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_recording_sessions_org_created_id",
            "recording_sessions",
            ["organization_id", sa.text("created_at DESC"), sa.text("id DESC")],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_recording_sessions_org_created_id",
            table_name="recording_sessions",
            postgresql_concurrently=True,
        )
    op.drop_column("recording_sessions", "speaker_count")
