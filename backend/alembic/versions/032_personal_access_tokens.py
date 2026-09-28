"""Personal access tokens for per-user MCP authentication.

Revision ID: 032_personal_access_tokens
Revises: 031_session_audio_object_storage
Create Date: 2026-05-28 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "032_personal_access_tokens"
down_revision = "031_session_audio_object_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_personal_access_tokens_token_hash",
        ),
    )
    op.create_index(
        "ix_personal_access_tokens_token_hash",
        "personal_access_tokens",
        ["token_hash"],
    )
    op.create_index(
        "ix_personal_access_tokens_user_id",
        "personal_access_tokens",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_personal_access_tokens_user_id", table_name="personal_access_tokens")
    op.drop_index("ix_personal_access_tokens_token_hash", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
