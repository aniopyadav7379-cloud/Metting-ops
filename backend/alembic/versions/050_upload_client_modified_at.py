"""Carry the browser-reported file last-modified date for uploads.

Adds ``upload_jobs.client_modified_at`` (nullable timestamptz). The browser
File API only exposes ``File.lastModified`` (there is no OS *creation* date over
the web), so the frontend sends it at ``/api/uploads/start`` and the pipeline
feeds it into the meeting-provenance "recorded at" estimate — ranked above the
server file mtime (which, for an upload, is just the ingest time) and below an
explicit filename date or the audio container's embedded ``creation_time``. Lets
a file with no embedded timestamp and no date in its name default its
``meeting_date`` to when the file was actually last saved, not the upload time.

Additive + safe: new nullable column; existing rows and the live-recording /
bulk-import paths (which never set it) are unaffected.

Revision ID: 050_upload_client_modified_at
Revises: 049_room_retention_opt_in
"""
from alembic import op
import sqlalchemy as sa

revision = "050_upload_client_modified_at"
down_revision = "049_room_retention_opt_in"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "upload_jobs",
        sa.Column("client_modified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_jobs", "client_modified_at")
