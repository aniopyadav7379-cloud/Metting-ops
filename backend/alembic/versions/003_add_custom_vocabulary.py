"""Add custom vocabulary tables.

Revision ID: 003_custom_vocabulary
Revises: 002_schema_placeholders
Create Date: 2025-01-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "003_custom_vocabulary"
down_revision = "002_schema_placeholders"
branch_labels = None
depends_on = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _constraint_exists(inspector, table_name: str, constraint_name: str) -> bool:
    return any(
        constraint.get("name") == constraint_name
        for constraint in inspector.get_unique_constraints(table_name)
    )


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "custom_vocabulary"):
        op.create_table(
            "custom_vocabulary",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("term", sa.String(100), nullable=False),
            sa.Column("expansion", sa.String(500), nullable=False),
            sa.Column("category", sa.String(50), nullable=True),
            sa.Column("industry", sa.String(50), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=True),
            sa.Column("context_hints", postgresql.ARRAY(sa.Text()), nullable=True),
            sa.Column("case_sensitive", sa.Boolean(), nullable=True),
            sa.Column("regex_pattern", sa.String(200), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("usage_count", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        inspector = sa.inspect(bind)

    if not _constraint_exists(inspector, "custom_vocabulary", "uq_vocabulary_term_category"):
        op.create_unique_constraint(
            "uq_vocabulary_term_category",
            "custom_vocabulary",
            ["term", "category"],
        )

    if not _table_exists(inspector, "vocabulary_sets"):
        op.create_table(
            "vocabulary_sets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=True),
            sa.Column("industry", sa.String(50), nullable=True),
            sa.Column("category", sa.String(50), nullable=True),
            sa.Column("vocab_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
            sa.Column("settings", postgresql.JSONB, nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        inspector = sa.inspect(bind)

    if not _constraint_exists(inspector, "vocabulary_sets", "vocabulary_sets_name_key"):
        op.create_unique_constraint("vocabulary_sets_name_key", "vocabulary_sets", ["name"])

    if not _table_exists(inspector, "session_vocabulary"):
        op.create_table(
            "session_vocabulary",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("vocabulary_set_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("applied_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("applied_to_live", sa.Boolean(), nullable=True),
            sa.Column("applied_to_final", sa.Boolean(), nullable=True),
        )
        op.create_foreign_key(
            "fk_session_vocabulary_set",
            "session_vocabulary",
            "vocabulary_sets",
            ["vocabulary_set_id"],
            ["id"],
            ondelete="CASCADE",
        )
        inspector = sa.inspect(bind)

    if not _index_exists(inspector, "custom_vocabulary", "idx_vocabulary_term"):
        op.create_index("idx_vocabulary_term", "custom_vocabulary", ["term"])
    if not _index_exists(inspector, "custom_vocabulary", "idx_vocabulary_category"):
        op.create_index("idx_vocabulary_category", "custom_vocabulary", ["category"])
    if not _index_exists(inspector, "custom_vocabulary", "idx_vocabulary_active"):
        op.create_index("idx_vocabulary_active", "custom_vocabulary", ["is_active"])
    if not _index_exists(inspector, "vocabulary_sets", "idx_vocabulary_sets_active"):
        op.create_index("idx_vocabulary_sets_active", "vocabulary_sets", ["is_active"])
    if not _index_exists(inspector, "session_vocabulary", "idx_session_vocabulary_session"):
        op.create_index("idx_session_vocabulary_session", "session_vocabulary", ["session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _index_exists(inspector, "session_vocabulary", "idx_session_vocabulary_session"):
        op.drop_index("idx_session_vocabulary_session", table_name="session_vocabulary")
    if _index_exists(inspector, "vocabulary_sets", "idx_vocabulary_sets_active"):
        op.drop_index("idx_vocabulary_sets_active", table_name="vocabulary_sets")
    if _index_exists(inspector, "custom_vocabulary", "idx_vocabulary_active"):
        op.drop_index("idx_vocabulary_active", table_name="custom_vocabulary")
    if _index_exists(inspector, "custom_vocabulary", "idx_vocabulary_category"):
        op.drop_index("idx_vocabulary_category", table_name="custom_vocabulary")
    if _index_exists(inspector, "custom_vocabulary", "idx_vocabulary_term"):
        op.drop_index("idx_vocabulary_term", table_name="custom_vocabulary")

    if _table_exists(inspector, "session_vocabulary"):
        op.drop_table("session_vocabulary")
    if _table_exists(inspector, "vocabulary_sets"):
        op.drop_table("vocabulary_sets")
    if _table_exists(inspector, "custom_vocabulary"):
        op.drop_table("custom_vocabulary")
