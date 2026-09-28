"""Session attachments: file uploads attached to a recording session.

Revision ID: 025_session_attachments
Revises: 024_satellite_device_secret
Create Date: 2026-05-20 00:00:00.000000

Lets users attach related files to a session record beyond the primary
audio capture — Granola-style notes from a coworker, an external Otter
transcript, a slide deck, a photo of a whiteboard, a PDF agenda, etc.

Storage is pluggable via the `storage_backend` column:

    'garage' — bucket=meeting-ops-attachments, key={org_id}/{session_id}/{uuid}/{filename}
    'local'  — file lives under RECORDINGS_DIR/attachments/{org_id}/{session_id}/{uuid}/{filename}
    'forgejo' — reserved for future when we mirror docs into a git repo

For v1 the writer prefers Garage (we have it on bigboy) and falls back
to local when GARAGE_ENDPOINT_URL isn't configured. The reader keys off
`storage_backend` so both work side-by-side without a migration.

`attachment_type` is a free-form short label the UI uses to filter and
pick an icon: 'transcript', 'notes', 'document', 'audio', 'image',
'video', 'other'. Not constrained by CHECK constraint because adding
new types should not require a migration.

Cascade on session delete — when the recording session is removed, the
attachment rows go with it (storage cleanup is handled by the API DELETE
handler; if a session is hard-deleted via SQL the bytes are leaked, but
that's also the case for the parent audio file today).
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa


logger = logging.getLogger("alembic.runtime.migration")

revision = "025_session_attachments"
down_revision = "024_satellite_device_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "session_attachments" not in existing_tables:
        op.create_table(
            "session_attachments",
            # UUID PK so the API can mint the id client-side for optimistic
            # UI without a round-trip to learn the integer pk.
            sa.Column(
                "id",
                sa.String(length=36),
                primary_key=True,
            ),
            sa.Column(
                "session_id",
                sa.Integer(),
                sa.ForeignKey("recording_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "uploaded_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("filename", sa.String(length=500), nullable=False),
            sa.Column("mime_type", sa.String(length=200), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            # Loose enum — see module docstring for current set.
            sa.Column("attachment_type", sa.String(length=50), nullable=False),
            sa.Column("source_label", sa.String(length=200), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("storage_backend", sa.String(length=20), nullable=False),
            sa.Column("storage_key", sa.String(length=500), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    # Indexes (idempotent so a stray Base.metadata.create_all() doesn't
    # collide with the explicit names).
    existing_indexes = {
        ix["name"] for ix in inspector.get_indexes("session_attachments")
    } if "session_attachments" in inspector.get_table_names() else set()
    if "ix_session_attachments_session" not in existing_indexes:
        op.create_index(
            "ix_session_attachments_session",
            "session_attachments",
            ["session_id"],
        )
    if "ix_session_attachments_organization" not in existing_indexes:
        op.create_index(
            "ix_session_attachments_organization",
            "session_attachments",
            ["organization_id"],
        )
    if "ix_session_attachments_org_session" not in existing_indexes:
        op.create_index(
            "ix_session_attachments_org_session",
            "session_attachments",
            ["organization_id", "session_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_session_attachments_org_session", table_name="session_attachments"
    )
    op.drop_index(
        "ix_session_attachments_organization", table_name="session_attachments"
    )
    op.drop_index("ix_session_attachments_session", table_name="session_attachments")
    op.drop_table("session_attachments")
