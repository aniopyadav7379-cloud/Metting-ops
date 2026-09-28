"""Conference Room SQLAlchemy models.

Kept in a separate module from `database.models` so the room schema can
evolve without forcing a re-read of the 600-line core models file. Models
here share the same `Base.metadata` as the rest of the app (imported from
database.models), so alembic autogenerate sees them automatically once
this module is imported (handled by `alembic/env.py`).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from database.models import Base


# Allowed status enum-likes. Kept as Python constants rather than a DB
# CHECK so we can extend without migrations; the API validates against
# these sets.
ROOM_STATUSES = {"idle", "recording", "error", "disabled"}
ROOM_SOURCE_STATUSES = {"idle", "recording", "error", "disabled"}
ROOM_HARDWARE_TYPES = {
    "server_usb_mic",
    "satellite_device",
    "network_stream",
    "companion_app",
}
ROOM_ROLES = {"admin", "member", "viewer"}  # extensible at the API layer


class ConferenceRoom(Base):
    """A physical conference room owned by a single org.

    Holds the metadata + scheduling config; audio sources live in
    `room_audio_sources` (N per room) and per-room access grants live in
    `room_acl`. A room can have 0..N audio sources and 0..N ACL rows.
    """

    __tablename__ = "conference_rooms"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_conference_rooms_org_name",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    location = Column(String(500), nullable=True)
    status = Column(
        String(32),
        nullable=False,
        default="idle",
        server_default="idle",
    )
    default_retention_days = Column(
        Integer,
        nullable=False,
        default=90,
        server_default="90",
    )
    # Explicit per-room retention opt-in. `default_retention_days` carries a
    # server_default of 90, so honoring it unconditionally would silently purge
    # every room-attached session at 90 days even when the org set NO retention
    # policy. The scheduled purge (services/data_retention.py) only uses a room's
    # horizon when this flag is set; otherwise the session falls back to the
    # org-level policy (or no purge). Default off → no implicit deletes.
    retention_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    schedule_json = Column(JSONB, nullable=True)
    legal_hold = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class RoomAudioSource(Base):
    """One audio source attached to a conference room.

    `hardware_type` discriminates between local USB mic, remote satellite,
    network stream, and companion app. `device_path` is set for server USB
    mics (`hw:X,0`). `device_id` is set when the source is a registered
    satellite device.
    """

    __tablename__ = "room_audio_sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    room_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conference_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    hardware_type = Column(String(32), nullable=False)
    device_path = Column(String(255), nullable=True)
    # satellite_devices.id is Integer (legacy).
    device_id = Column(
        Integer,
        ForeignKey("satellite_devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    label = Column(String(255), nullable=True)
    status = Column(
        String(32),
        nullable=False,
        default="idle",
        server_default="idle",
    )
    config_json = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class RoomPairingCode(Base):
    """Pairing code issued to enrol a device into a room.

    6-digit zero-padded numeric. Generated codes are unique while still
    active (not redeemed, not expired). Once redeemed (or expired) the
    code value can be reissued. The API enforces the 10-minute TTL.
    """

    __tablename__ = "room_pairing_codes"
    __table_args__ = (
        # Partial index — only "active" codes (not yet redeemed) are
        # constrained for uniqueness. Matches the alembic migration's
        # idx_pairing_active index exactly so autogenerate doesn't drift.
        Index(
            "idx_pairing_active",
            "code",
            "expires_at",
            postgresql_where=text("redeemed_at IS NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    room_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conference_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code = Column(String(6), nullable=False, index=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    expires_at = Column(DateTime, nullable=False)
    redeemed_at = Column(DateTime, nullable=True)
    redeemed_by_device_id = Column(
        Integer,
        ForeignKey("satellite_devices.id", ondelete="SET NULL"),
        nullable=True,
    )


class RoomAcl(Base):
    """Per-room access grant.

    Augments the org-level role. Org admins always pass `can_access_room`
    regardless of whether they have a row here. A non-admin org member
    needs an explicit grant to read or mutate the room. `role` is a plain
    string ("admin" / "member" / "viewer" today; extensible without a
    migration).
    """

    __tablename__ = "room_acl"
    __table_args__ = (
        UniqueConstraint(
            "room_id",
            "user_id",
            name="uq_room_acl_room_user",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    room_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conference_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # users.id is Integer.
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(32), nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
