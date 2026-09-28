"""federation: organizations.workspace_id + GIN index on session participants

Revision ID: 041_federation_workspace_participant
Revises: 040_meeting_digest_emailed_at
Create Date: 2026-06-05

Two additive, online-safe changes backing the inbound Customer-Ops
federation reads (api/federation_meetings.py):

  1. organizations.workspace_id — the uc-registry workspace UUID that a
     Brigade federation token asserts (claim ``workspace_id``). The
     inbound verifier binds tenant from THIS column, never a header.
     Nullable + unique (Postgres allows multiple NULLs) so every
     existing org is untouched until explicitly mapped to its workspace.

  2. GIN index on recording_sessions.participants — makes the
     ``participants @> [{"contact_id": X}]`` containment lookup that
     powers the contact-centric reads index-assisted instead of a scan.
"""

from alembic import op
import sqlalchemy as sa


revision = "041_federation_workspace_participant"
down_revision = "040_meeting_digest_emailed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_organizations_workspace_id",
        "organizations",
        ["workspace_id"],
        unique=True,
    )
    op.create_index(
        "ix_recording_sessions_participants_gin",
        "recording_sessions",
        ["participants"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recording_sessions_participants_gin",
        table_name="recording_sessions",
    )
    op.drop_index(
        "ix_organizations_workspace_id",
        table_name="organizations",
    )
    op.drop_column("organizations", "workspace_id")
