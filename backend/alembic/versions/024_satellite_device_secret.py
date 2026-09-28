"""Per-device authentication secret for satellite devices.

Revision ID: 024_satellite_device_secret
Revises: 023_conference_rooms
Create Date: 2026-05-20 00:00:00.000000

Adds a per-device authentication secret (``device_secret``) to
``satellite_devices``. The column stores a bcrypt hash — the plaintext
secret is returned exactly once at pairing-code redemption time and never
again. This closes the long-standing gap where the satellite WebSocket
endpoint and several state-mutating HTTP endpoints authenticated by
``device_id`` alone — anyone who learned a device_id could stream audio
into the org's meeting transcript or trigger billable LLM calls.

Design notes (Aaron, 2026-05-20):
  * Nullable to permit clean autogenerate against existing rows; the
    application layer treats NULL as "must re-pair" — the WebSocket
    refuses any connection where the device has no secret on file.
  * No new index needed — auth lookups always hit the row by
    ``device_id`` (already indexed via the column-level unique
    constraint), then compare the hash in-memory.
  * No production satellite hardware has been deployed yet, so the
    backfill is intentionally a no-op. Any pre-existing rows (left over
    from dev/test) will be unable to authenticate until they re-pair —
    matches the security goal: never trust a device without a secret.

This migration is paired with the application-side flow in
``backend/api/rooms.py`` (pairing redemption issues the plaintext
secret), ``backend/api/websocket_satellite.py`` (verifies the secret on
connect), and ``backend/api/satellite_api.py`` (verifies the secret on
state-mutating HTTP endpoints).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "024_satellite_device_secret"
down_revision = "023_conference_rooms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # bcrypt hashes are 60 chars but we leave room for the algorithm to
    # change without another migration. Nullable: existing rows have no
    # secret on file and cannot authenticate to the WebSocket / state-
    # mutating HTTP endpoints until they re-pair. There are no production
    # satellite devices yet, so no human-visible regression.
    op.add_column(
        "satellite_devices",
        sa.Column("device_secret", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("satellite_devices", "device_secret")
