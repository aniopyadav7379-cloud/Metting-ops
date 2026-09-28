"""drop orphan meeting_chunk_embedding table

Revision ID: b18939734458
Revises: 021_action_items
Create Date: 2026-05-19 22:32:45.396563

Cleanup after the meet_chunks pipeline removal in commit 8dec955.
The meeting_chunk_embedding table tracked Qdrant chunk embeddings for the
deleted /api/rag/chat pipeline. The canonical meeting_transcripts pipeline
covers the use case, leaving this table with no writer and no reader.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b18939734458'
down_revision = '021_action_items'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_meeting_chunk_embedding_meeting_id", table_name="meeting_chunk_embedding")
    op.drop_table("meeting_chunk_embedding")


def downgrade() -> None:
    op.create_table(
        "meeting_chunk_embedding",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("chunk_idx", sa.Integer(), nullable=False),
        sa.Column("qdrant_point_id", sa.String(length=100), nullable=False),
        sa.Column("text_snippet", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["meeting_id"], ["recording_sessions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_meeting_chunk_embedding_meeting_id",
        "meeting_chunk_embedding",
        ["meeting_id"],
    )
