"""Add hashed invitation secrets and delivery state.

Revision ID: 053_invitation_hashes_delivery
Revises: 052_beta_invite_codes_emailed_at

This is the compatibility stage. Existing UUID secrets are hashed while the
legacy column remains intact so the old application can continue serving
traffic until the new application is ready. The irreversible plaintext scrub
is a separate approval-gated SQL operation, not an Alembic head revision.
"""
from alembic import op
import sqlalchemy as sa


revision = "053_invitation_hashes_delivery"
down_revision = "052_beta_invite_codes_emailed_at"
branch_labels = None
depends_on = None


_DELIVERY_STATES = (
    "pending",
    "sent",
    "failed",
    "accepted",
    "revoked",
    "expired",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.add_column(
        "session_collaborators",
        sa.Column("token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "session_collaborators",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=True,
            server_default="2",
        ),
    )
    op.add_column(
        "session_collaborators",
        sa.Column(
            "delivery_state",
            sa.String(length=20),
            nullable=True,
            server_default="pending",
        ),
    )
    op.add_column(
        "session_collaborators",
        sa.Column(
            "delivery_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "session_collaborators",
        sa.Column(
            "last_delivery_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "session_collaborators",
        sa.Column("delivery_failure_reason", sa.String(length=120), nullable=True),
    )

    op.execute(
        """
        UPDATE session_collaborators
        SET token_hash = encode(digest(token::text, 'sha256'), 'hex'),
            token_version = 1,
            delivery_state = CASE
                WHEN revoked_at IS NOT NULL THEN 'revoked'
                WHEN user_id IS NOT NULL OR accepted_at IS NOT NULL
                    THEN 'accepted'
                WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 'expired'
                ELSE 'sent'
            END
        WHERE token IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE session_collaborators
        SET token_version = COALESCE(token_version, 2),
            delivery_state = COALESCE(delivery_state, 'pending')
        """
    )

    op.alter_column("session_collaborators", "token_version", nullable=False)
    op.alter_column("session_collaborators", "delivery_state", nullable=False)
    op.create_check_constraint(
        "ck_session_collaborators_delivery_state",
        "session_collaborators",
        "delivery_state IN ('pending', 'sent', 'failed', 'accepted', 'revoked', 'expired')",
    )
    op.create_check_constraint(
        "ck_session_collaborators_token_version",
        "session_collaborators",
        "token_version IN (1, 2)",
    )
    op.create_index(
        "ix_session_collaborators_token_hash",
        "session_collaborators",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_collaborators_token_hash",
        table_name="session_collaborators",
    )
    op.drop_constraint(
        "ck_session_collaborators_token_version",
        "session_collaborators",
        type_="check",
    )
    op.drop_constraint(
        "ck_session_collaborators_delivery_state",
        "session_collaborators",
        type_="check",
    )
    op.drop_column("session_collaborators", "delivery_failure_reason")
    op.drop_column("session_collaborators", "last_delivery_attempt_at")
    op.drop_column("session_collaborators", "delivery_attempt_count")
    op.drop_column("session_collaborators", "delivery_state")
    op.drop_column("session_collaborators", "token_version")
    op.drop_column("session_collaborators", "token_hash")
    # pgcrypto is intentionally retained because other deployments may use it.
