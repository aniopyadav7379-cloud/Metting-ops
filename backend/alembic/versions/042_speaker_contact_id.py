"""speaker contact id link

Revision ID: 042_speaker_contact_id
Revises: 041_federation_workspace_participant
Create Date: 2026-06-06 00:35:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "042_speaker_contact_id"
down_revision = "041_federation_workspace_participant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speaker", sa.Column("contact_id", sa.String(length=64), nullable=True))
    op.add_column(
        "speaker",
        sa.Column("contact_link_confirmed", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("speaker", sa.Column("contact_link_confirmed_by_user_id", sa.Integer(), nullable=True))
    op.add_column("speaker", sa.Column("contact_link_confirmed_at", sa.DateTime(), nullable=True))
    op.create_foreign_key(
        "speaker_contact_link_confirmed_by_fkey",
        "speaker",
        "users",
        ["contact_link_confirmed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_speaker_contact_id", "speaker", ["contact_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_speaker_contact_id", table_name="speaker")
    op.drop_constraint("speaker_contact_link_confirmed_by_fkey", "speaker", type_="foreignkey")
    op.drop_column("speaker", "contact_link_confirmed_at")
    op.drop_column("speaker", "contact_link_confirmed_by_user_id")
    op.drop_column("speaker", "contact_link_confirmed")
    op.drop_column("speaker", "contact_id")
