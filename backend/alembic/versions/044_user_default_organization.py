"""user default (home) organization

Adds `users.default_organization_id` — the per-user "home" workspace
(Organization). When set AND the caller is still a member of it, this is
the top preference `auth.organization.resolve_active_organization` falls
back to when no `X-MeetingOps-Org` header / `?org=` selector is supplied.
It replaces the dogfood-specific DEFAULT_ORG_SLUG ("magic-unicorn")
preference hack, so a user's chosen workspace persists server-side and
works across devices + on any node (not just the magic-unicorn node).

Set via `PUT /api/organizations/default`. NULL means "no preference"
(fall through to the existing fallback chain). The FK is ON DELETE SET
NULL so deleting an organization clears the preference rather than
orphaning the user row.

Revision ID: 044_user_default_org
Revises: 043_speaker_photo_url
Create Date: 2026-06-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "044_user_default_org"
down_revision = "043_speaker_photo_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("default_organization_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "users_default_organization_id_fkey",
        "users",
        "organizations",
        ["default_organization_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "users_default_organization_id_fkey",
        "users",
        type_="foreignkey",
    )
    op.drop_column("users", "default_organization_id")
