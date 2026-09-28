"""Phase 3: Speaker library — speaker, speaker_voice_sample, speaker_session_link.

Embeddings stored as raw bytes + a dim column so we can swap models
(ECAPA 192-d -> ECAPA-XL 256-d -> WavLM 768-d) without a schema migration.

Revision ID: 006_speakers
Revises: 005_phase2_rag
Create Date: 2026-05-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "006_speakers"
down_revision = "005_phase2_rag"
branch_labels = None
depends_on = None


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = _is_postgres(bind)
    bytea_type = sa.LargeBinary() if not is_pg else sa.dialects.postgresql.BYTEA()

    # ---------- speaker ----------
    op.create_table(
        "speaker",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("centroid_embedding", bytea_type, nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_speaker_org", "speaker", ["organization_id"])
    op.create_index(
        "ix_speaker_org_name",
        "speaker",
        ["organization_id", "display_name"],
        unique=True,
    )

    # ---------- speaker_voice_sample ----------
    # One row per enrollment clip / per identified meeting segment used to
    # update the centroid. Audio bytes are optional (privacy-first default).
    op.create_table(
        "speaker_voice_sample",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("speaker_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),  # enrollment | session
        sa.Column("source_session_id", sa.Integer(), nullable=True),
        sa.Column("source_segment_idx", sa.Integer(), nullable=True),
        sa.Column("embedding", bytea_type, nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("audio_path", sa.String(length=500), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("similarity_to_centroid", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["speaker_id"], ["speaker.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_session_id"], ["recording_sessions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_speaker_voice_sample_speaker", "speaker_voice_sample", ["speaker_id"])
    op.create_index("ix_speaker_voice_sample_org", "speaker_voice_sample", ["organization_id"])
    op.create_index("ix_speaker_voice_sample_session", "speaker_voice_sample", ["source_session_id"])

    # ---------- speaker_session_link ----------
    # Links a recording_session's diarized speaker label (e.g. "SPEAKER_00")
    # to an org-level Speaker. One row per unique (session, raw_label).
    op.create_table(
        "speaker_session_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("raw_label", sa.String(length=100), nullable=False),
        sa.Column("speaker_id", sa.Integer(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="auto"),  # auto | manual
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("0") if not is_pg else sa.text("false")),
        sa.Column("confirmed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["recording_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["speaker_id"], ["speaker.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmed_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_speaker_session_link_session", "speaker_session_link", ["session_id"])
    op.create_index("ix_speaker_session_link_org", "speaker_session_link", ["organization_id"])
    op.create_index("ix_speaker_session_link_speaker", "speaker_session_link", ["speaker_id"])
    op.create_index(
        "uq_speaker_session_link_label",
        "speaker_session_link",
        ["session_id", "raw_label"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_speaker_session_link_label", table_name="speaker_session_link")
    op.drop_index("ix_speaker_session_link_speaker", table_name="speaker_session_link")
    op.drop_index("ix_speaker_session_link_org", table_name="speaker_session_link")
    op.drop_index("ix_speaker_session_link_session", table_name="speaker_session_link")
    op.drop_table("speaker_session_link")

    op.drop_index("ix_speaker_voice_sample_session", table_name="speaker_voice_sample")
    op.drop_index("ix_speaker_voice_sample_org", table_name="speaker_voice_sample")
    op.drop_index("ix_speaker_voice_sample_speaker", table_name="speaker_voice_sample")
    op.drop_table("speaker_voice_sample")

    op.drop_index("ix_speaker_org_name", table_name="speaker")
    op.drop_index("ix_speaker_org", table_name="speaker")
    op.drop_table("speaker")
