"""speaker_profile contact fields

Revision ID: 015_speaker_profile_contacts
Revises: 014_session_collaborators
Create Date: 2026-05-16 22:00:00.000000

Adds the contact-record fields described in the SpeakerProfile model
docstring: phone, title, company, linked_user_id (FK), external_refs
(JSONB).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "015_speaker_profile_contacts"
down_revision = "014_session_collaborators"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speaker", sa.Column("phone", sa.String(length=64), nullable=True))
    op.add_column("speaker", sa.Column("title", sa.String(length=200), nullable=True))
    op.add_column("speaker", sa.Column("company", sa.String(length=200), nullable=True))
    op.add_column(
        "speaker",
        sa.Column("linked_user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "speaker",
        sa.Column("external_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        "speaker_linked_user_fkey",
        "speaker",
        "users",
        ["linked_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_speaker_linked_user", "speaker", ["linked_user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_speaker_linked_user", table_name="speaker")
    op.drop_constraint("speaker_linked_user_fkey", "speaker", type_="foreignkey")
    op.drop_column("speaker", "external_refs")
    op.drop_column("speaker", "linked_user_id")
    op.drop_column("speaker", "company")
    op.drop_column("speaker", "title")
    op.drop_column("speaker", "phone")
