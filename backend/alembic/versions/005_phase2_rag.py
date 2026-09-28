"""Add Provider Registry, meeting embeddings, project linking, and digests tables.

Revision ID: 005_phase2_rag
Revises: 004_multi_org_scoping
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "005_phase2_rag"
down_revision = "004_multi_org_scoping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_provider_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("service_kind", sa.String(50), nullable=False),
        sa.Column("provider_name", sa.String(50), nullable=False),
        sa.Column("endpoint_url", sa.String(500), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(200), nullable=True),
        sa.Column("overrides", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_org_provider_settings_org_kind",
        "org_provider_settings",
        ["organization_id", "service_kind"],
        unique=True,
    )

    op.create_table(
        "meeting_chunk_embedding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("chunk_idx", sa.Integer(), nullable=False),
        sa.Column("qdrant_point_id", sa.String(100), nullable=False),
        sa.Column("text_snippet", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["recording_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_chunk_embedding_meeting_id", "meeting_chunk_embedding", ["meeting_id"])

    op.create_table(
        "meeting_digest",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(20), nullable=False),
        sa.Column("date", sa.String(10), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("meeting_count", sa.Integer(), default=0),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meeting_digest_org_period", "meeting_digest", ["organization_id", "period", "date"])

    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("recording_sessions")}

    if "project_app" not in columns:
        op.add_column("recording_sessions", sa.Column("project_app", sa.String(50), nullable=True))
    if "project_id" not in columns:
        op.add_column("recording_sessions", sa.Column("project_id", sa.Integer(), nullable=True))
    if "project_slug" not in columns:
        op.add_column("recording_sessions", sa.Column("project_slug", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_meeting_digest_org_period", table_name="meeting_digest")
    op.drop_table("meeting_digest")
    op.drop_index("ix_meeting_chunk_embedding_meeting_id", table_name="meeting_chunk_embedding")
    op.drop_table("meeting_chunk_embedding")
    op.drop_index("ix_org_provider_settings_org_kind", table_name="org_provider_settings")
    op.drop_table("org_provider_settings")

    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("recording_sessions")}
    for col in ("project_slug", "project_id", "project_app"):
        if col in columns:
            op.drop_column("recording_sessions", col)
