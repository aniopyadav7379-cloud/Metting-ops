"""Brigade graph wiring on recording_sessions.

Revision ID: 030_brigade_graph_node_id
Revises: 029_bulk_import
Create Date: 2026-05-22 18:00:00.000000

Adds two columns + one index to recording_sessions so the new Brigade
writer service can record what it wrote and the reconciliation job can
find unsynced sessions cheaply.

  brigade_graph_node_id  VARCHAR(64) NULL
      The entity name we passed to Brigade's /knowledge/store/entity
      for the Meeting node (canonical key, stable across re-writes).
      Today the writer derives the value as f"meeting_ops_meeting_{session.id}".
      NULL means the session has never been synced.

  brigade_synced_at      TIMESTAMP NULL
      Wall-clock when the last successful write completed. Drives the
      "View in Brigade graph" link on SessionDetails (we only surface
      the link when this is non-NULL) and the reconciliation job's
      "find sessions completed in the last 7 days that never got a
      Brigade write" query.

Index on brigade_synced_at is the predicate the reconcile_brigade_graph.py
job uses ("WHERE brigade_synced_at IS NULL AND status = 'completed'");
small table so the index is cheap.

Down is reversible (drop index + columns). Postgres-only production
target; SQLite test fixture takes the same DDL via standard types.

Foundation for Brigade integration Phase 1 per
docs/brigade-integration-design.md. Writer service is at
backend/services/brigade_writer.py.
"""

from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa


revision = "030_brigade_graph_node_id"
down_revision = "029_bulk_import"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.030_brigade_graph_node_id")


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("brigade_graph_node_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "recording_sessions",
        sa.Column("brigade_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Index drives the "find unsynced sessions" reconciliation query.
    # Partial index on Postgres would be even better but SQLite (test
    # fixture) doesn't take WHERE on indexes; keep it portable.
    op.create_index(
        "ix_recording_sessions_brigade_synced_at",
        "recording_sessions",
        ["brigade_synced_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recording_sessions_brigade_synced_at",
        table_name="recording_sessions",
    )
    op.drop_column("recording_sessions", "brigade_synced_at")
    op.drop_column("recording_sessions", "brigade_graph_node_id")
