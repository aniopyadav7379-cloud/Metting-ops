"""speaker photo url (Meeting-Ops-local avatar override)

A per-speaker portrait URL the user can set inside Meeting-Ops. This is the
HIGHEST-priority source in the avatar chain (MO override > Contact-Ops
federated photo > generated initials), so a user can always pin a face even
when the speaker isn't linked to a Contact-Ops person.

Revision ID: 043_speaker_photo_url
Revises: 042_speaker_contact_id
Create Date: 2026-06-07 13:55:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "043_speaker_photo_url"
down_revision = "042_speaker_contact_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("speaker", sa.Column("photo_url", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("speaker", "photo_url")
