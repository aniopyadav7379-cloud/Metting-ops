"""per-meeting collaborators (session_collaborators)

Revision ID: 014_session_collaborators
Revises: 013_session_title_user_set
Create Date: 2026-05-16 00:00:00.000000

Adds a `session_collaborators` table so a single recording session can be
shared with a specific user (by user_id, when they have a uchub
Keycloak account in the local `users` table) or an external invitee
(by email + magic-link token). Three access levels: read / comment / edit.

This is the per-session leaf of the broader RBAC plan in RBAC_DESIGN.md.
Org-level invitations and shares are intentionally NOT covered by this
migration — only per-meeting collaborators land here.

Requires the CITEXT extension for case-insensitive email matching.
"""

from alembic import op
import sqlalchemy as sa


revision = "014_session_collaborators"
down_revision = "013_session_title_user_set"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure CITEXT is available for case-insensitive email matching.
    # Idempotent: safe to re-run on databases that already have it.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "session_collaborators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("recording_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email", sa.dialects.postgresql.CITEXT(), nullable=True),
        sa.Column(
            "access_level",
            sa.String(length=20),
            nullable=False,
            server_default="read",
        ),
        sa.Column(
            "invited_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "token",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "user_id IS NOT NULL OR email IS NOT NULL",
            name="ck_session_collaborators_user_or_email",
        ),
        sa.CheckConstraint(
            "access_level IN ('read', 'comment', 'edit')",
            name="ck_session_collaborators_access_level",
        ),
    )

    op.create_index(
        "ix_session_collaborators_session_user",
        "session_collaborators",
        ["session_id", "user_id"],
    )
    op.create_index(
        "ix_session_collaborators_session_email",
        "session_collaborators",
        ["session_id", "email"],
    )
    op.create_index(
        "ix_session_collaborators_token",
        "session_collaborators",
        ["token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_collaborators_token", table_name="session_collaborators"
    )
    op.drop_index(
        "ix_session_collaborators_session_email",
        table_name="session_collaborators",
    )
    op.drop_index(
        "ix_session_collaborators_session_user",
        table_name="session_collaborators",
    )
    op.drop_table("session_collaborators")
    # Intentionally do NOT drop the CITEXT extension — other tables may
    # adopt it in future migrations and it's harmless to leave installed.
