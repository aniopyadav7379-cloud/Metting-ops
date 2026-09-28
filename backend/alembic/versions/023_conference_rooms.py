"""Conference Room recording — Phase 1 schema.

Revision ID: 023_conference_rooms
Revises: 022_stt_default_parakeet
Create Date: 2026-05-20 00:00:00.000000

Lands the data model for the Conference Room feature:

  conference_rooms        — one row per physical room.
  room_audio_sources      — N audio sources per room (USB mic, satellite,
                            companion app, network stream). Multiple sources
                            on the same room is supported from day one (table
                            mic + ceiling mic).
  room_pairing_codes      — 6-digit numeric codes for satellite/companion
                            enrolment. 10-minute TTL, single-use, rate-limited
                            by the API.
  room_acl                — per-room access grants (admin/member/viewer; the
                            `role` column is intentionally a plain string so
                            new roles can be added without a migration).

Also extends `recording_sessions` with `room_id` + `room_source_id` so
room-originated meetings can be filtered/grouped without scanning
processing_metadata blobs.

Design decisions (Aaron, 2026-05-20):
  * Multi-room concurrent native — N rooms per backend instance.
  * No streaming protocol; rooms POST to /api/recordings/sessions/{id}/chunks
    just like browser users. Internal HTTPS only.
  * Server-attached USB mics are the Phase 1 primary deployment. Other
    hardware_type values exist on the table so the API stays generic.
  * APScheduler config goes in `schedule_json` JSONB for Phase 2 wiring;
    Phase 1 leaves the column nullable and unused.

Phase 3 (not landed here) will wire satellite enrolment + remote
streaming. The data model is built so that work is purely additive.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "023_conference_rooms"
down_revision = "022_stt_default_parakeet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- conference_rooms ----------------------------------------------------
    op.create_table(
        "conference_rooms",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("location", sa.String(500), nullable=True),
        # status values: idle | recording | error | disabled. Plain string
        # so future states (paused, scheduled) can be added without a
        # migration; the API enforces the allowed set.
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="idle",
        ),
        sa.Column(
            "default_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
        # APScheduler config — Phase 2 wires it. Stored as JSONB so future
        # additions (cron, day-of-week, exception dates) don't need
        # migrations.
        sa.Column("schedule_json", JSONB(), nullable=True),
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "name",
            name="uq_conference_rooms_org_name",
        ),
    )

    # --- room_audio_sources --------------------------------------------------
    # hardware_type values:
    #   server_usb_mic   — `arecord -D hw:X,0 ...` on the backend host
    #   satellite_device — ESP32/Pi satellite device (Phase 3 wiring)
    #   network_stream   — RTSP/SRT/Icecast (future)
    #   companion_app    — phone/laptop browser companion
    op.create_table(
        "room_audio_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conference_rooms.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("hardware_type", sa.String(32), nullable=False),
        sa.Column("device_path", sa.String(255), nullable=True),
        # satellite_devices.id is Integer (legacy). FK kept as Integer to
        # match; SET NULL on satellite delete so the source row survives.
        sa.Column(
            "device_id",
            sa.Integer(),
            sa.ForeignKey("satellite_devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="idle",
        ),
        sa.Column("config_json", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- room_pairing_codes --------------------------------------------------
    # 6-digit zero-padded numeric. Uniqueness is enforced per-active code
    # (not redeemed AND not expired) by the partial index below — the API
    # retries until it picks a free one. The full column is NOT globally
    # unique because once a code is redeemed (or expires) the same code
    # value can be re-issued for a different room.
    op.create_table(
        "room_pairing_codes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conference_rooms.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("code", sa.String(6), nullable=False, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "redeemed_by_device_id",
            sa.Integer(),
            sa.ForeignKey("satellite_devices.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Partial index: a code is "active" while not yet redeemed. The API uses
    # this to enforce uniqueness AT GENERATION TIME and to look up redemptions
    # without scanning expired/spent rows.
    op.create_index(
        "idx_pairing_active",
        "room_pairing_codes",
        ["code", "expires_at"],
        postgresql_where=sa.text("redeemed_at IS NULL"),
    )

    # --- room_acl ------------------------------------------------------------
    # role is a plain string (admin/member/viewer extensible). We do NOT
    # add a CHECK constraint so legal_officer, observer, etc. can be added
    # by API-level updates without a schema migration. The API validates
    # the role string against the current allowed set per request.
    op.create_table(
        "room_acl",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conference_rooms.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # users.id is Integer (auth/models.py). FK matches.
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "room_id",
            "user_id",
            name="uq_room_acl_room_user",
        ),
    )

    # --- recording_sessions extensions --------------------------------------
    # When a session was driven by a room recorder we stamp it here so the
    # session list/detail UI can show "from Conference Room X" and we can
    # filter analytics per-room without scanning processing_metadata JSON.
    # SET NULL on parent delete so deleting a room doesn't cascade-delete
    # historical recordings; the room reference simply disappears.
    op.add_column(
        "recording_sessions",
        sa.Column(
            "room_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conference_rooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_recording_sessions_room_id",
        "recording_sessions",
        ["room_id"],
    )
    op.add_column(
        "recording_sessions",
        sa.Column(
            "room_source_id",
            UUID(as_uuid=True),
            sa.ForeignKey("room_audio_sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Drop in reverse FK order. recording_sessions columns first so we can
    # drop the parent tables cleanly.
    op.drop_column("recording_sessions", "room_source_id")
    op.drop_index(
        "ix_recording_sessions_room_id",
        table_name="recording_sessions",
    )
    op.drop_column("recording_sessions", "room_id")

    op.drop_table("room_acl")

    op.drop_index("idx_pairing_active", table_name="room_pairing_codes")
    op.drop_table("room_pairing_codes")

    op.drop_table("room_audio_sources")

    op.drop_table("conference_rooms")
