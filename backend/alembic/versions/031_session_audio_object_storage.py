"""Durable object-storage location for session canonical audio.

Revision ID: 031_session_audio_object_storage
Revises: 030_brigade_graph_node_id
Create Date: 2026-05-27 21:00:00.000000

Adds two nullable columns to recording_sessions so the main meeting audio
can live in Garage (S3-compatible object storage) the same way session
attachments already do, instead of only on a single host's local disk.

  audio_storage_backend  VARCHAR(20) NULL
      'garage' | 'local' | NULL. Which backend holds the durable copy of
      this session's canonical audio. NULL = not yet pushed to object
      storage (the read path then falls back to the local `audio_file`
      column, so every pre-existing row keeps working unchanged).

  audio_object_key       VARCHAR(500) NULL
      The Garage object key, convention '{org_id}/{session_id}/audio/{name}'
      (see services/media_storage.py). NULL when backend is NULL/local.

Purely additive + nullable: zero behavior change on apply. The legacy
`audio_file` path stays the local working copy / source-of-truth during
cutover; these columns record the durable canonical copy. Free-tier audio
never populates these (browser-only). Down is reversible (drop columns).

Foundation for routing main audio through Garage — companion to the
existing attachment_storage.py / session_attachments storage columns.
"""

from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa


revision = "031_session_audio_object_storage"
down_revision = "030_brigade_graph_node_id"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.031_session_audio_object_storage")


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("audio_storage_backend", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "recording_sessions",
        sa.Column("audio_object_key", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recording_sessions", "audio_object_key")
    op.drop_column("recording_sessions", "audio_storage_backend")
