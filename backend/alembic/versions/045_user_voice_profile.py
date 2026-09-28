""""My Voice" — portable per-USER-account voiceprint (v3.36.0).

Adds `user_voice_profile`: ONE self-voiceprint per user account. Org
SpeakerProfile rows stay hard tenant-scoped (biometric privacy — no
cross-tenant matching, ever); this table is the single sanctioned
exception, and it only participates in identification inside workspaces
the user is a MEMBER of (identify joins through user_organizations).

`consent_at` is the biometric consent record — stamped ONLY by explicit
user action (an is_me claim while naming a speaker, or POST
/api/me/voice/enroll-from-session; never by implicit inference such as an
email match). Row deletion (DELETE /api/me/voice) destroys the voiceprint
(and recomputes org SpeakerProfile centroids seeded from it); users.id
ON DELETE CASCADE destroys it with the account.

Revision ID: 045_user_voice_profile
Revises: 044_user_default_org
Create Date: 2026-06-10 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "045_user_voice_profile"
down_revision = "044_user_default_org"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_voice_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("centroid_embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_user_voice_profile_user",
        "user_voice_profile",
        ["user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_user_voice_profile_user", table_name="user_voice_profile")
    op.drop_table("user_voice_profile")
