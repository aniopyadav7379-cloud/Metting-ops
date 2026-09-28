"""Bind integration PATs to a scope, organization, expiry, and rotation chain.

Revision ID: 059_integration_pat_scope
Revises: 058_stable_ingest
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "059_integration_pat_scope"
down_revision = "058_stable_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personal_access_tokens",
        sa.Column(
            "scope",
            sa.String(length=64),
            nullable=False,
            server_default="user",
        ),
    )
    op.add_column(
        "personal_access_tokens",
        sa.Column("organization_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "personal_access_tokens",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "personal_access_tokens",
        sa.Column("rotated_from_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_personal_access_tokens_organization",
        "personal_access_tokens",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_personal_access_tokens_rotated_from",
        "personal_access_tokens",
        "personal_access_tokens",
        ["rotated_from_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_personal_access_tokens_organization_id",
        "personal_access_tokens",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_access_tokens_organization_id",
        table_name="personal_access_tokens",
    )
    op.drop_constraint(
        "fk_personal_access_tokens_rotated_from",
        "personal_access_tokens",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_personal_access_tokens_organization",
        "personal_access_tokens",
        type_="foreignkey",
    )
    op.drop_column("personal_access_tokens", "rotated_from_id")
    op.drop_column("personal_access_tokens", "expires_at")
    op.drop_column("personal_access_tokens", "organization_id")
    op.drop_column("personal_access_tokens", "scope")
