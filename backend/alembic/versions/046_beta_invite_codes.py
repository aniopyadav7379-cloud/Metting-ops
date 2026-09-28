"""Phase-1 beta invite codes.

Adds `beta_invite_codes`: single-use (by default) self-serve-signup gate.
When auth_config.REQUIRE_INVITE_CODE is on, POST /api/auth/register
requires a valid code; redeeming it comps the new user's personal org to
Pro. Admins mint codes (POST /api/admin/invite-codes) and seed users hand
them out. Phase 2 (invitee self-mint) is NOT in this migration.

Additive only — no existing table is touched. The two user FKs are
ON DELETE SET NULL so deleting a user neither orphans the row nor cascades
the code away (the "this code was redeemed" audit fact outlives the
redeemer's account).

Revision ID: 046_beta_invite_codes
Revises: 045_user_voice_profile
Create Date: 2026-06-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "046_beta_invite_codes"
down_revision = "045_user_voice_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "beta_invite_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "max_redemptions", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "redemption_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "redeemed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_beta_invite_codes_code",
        "beta_invite_codes",
        ["code"],
        unique=True,
    )
    op.create_index(
        "ix_beta_invite_codes_created_by_user_id",
        "beta_invite_codes",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_beta_invite_codes_created_by_user_id",
        table_name="beta_invite_codes",
    )
    op.drop_index(
        "ix_beta_invite_codes_code", table_name="beta_invite_codes"
    )
    op.drop_table("beta_invite_codes")
