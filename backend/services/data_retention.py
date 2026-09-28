"""Scheduled compliance retention purge for canonical meeting data."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("MEETING_RETENTION_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def _env_days() -> int:
    try:
        return max(0, int(os.getenv("MEETING_RETENTION_DAYS", "0")))
    except ValueError:
        return 0


def run_data_retention_purge(*, dry_run: bool = False) -> dict:
    """Delete expired completed sessions, respecting room legal holds."""
    if not _enabled():
        return {"enabled": False, "deleted": 0}

    from api.simple_recording_db import _delete_session_record
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import ChatHistory, RecordingSession
    from database.models_rooms import ConferenceRoom

    cap = max(1, int(os.getenv("MEETING_RETENTION_MAX_PER_RUN", "500")))
    now = datetime.now(timezone.utc)
    result = {"enabled": True, "deleted": 0, "legal_hold_skipped": 0, "errors": 0, "dry_run": dry_run}
    db = SessionLocal()
    try:
        organizations = db.query(Organization).filter(Organization.is_active.is_(True)).all()
        for org in organizations:
            org_settings = org.settings if isinstance(org.settings, dict) else {}
            org_days = int(org_settings.get("retention_days", _env_days()) or 0)
            sessions = db.query(RecordingSession).filter(
                RecordingSession.organization_id == org.id,
                RecordingSession.status == "completed",
            ).order_by(RecordingSession.created_at).limit(cap - result["deleted"]).all()
            room_ids = {session.room_id for session in sessions if session.room_id}
            rooms = {
                room.id: room
                for room in db.query(ConferenceRoom).filter(ConferenceRoom.id.in_(room_ids)).all()
            } if room_ids else {}
            for session in sessions:
                room = rooms.get(session.room_id)
                if room and room.legal_hold:
                    result["legal_hold_skipped"] += 1
                    continue
                # Retention landmine fix: a room's default_retention_days has a
                # server_default of 90, so honoring it unconditionally would purge
                # every room-attached session at 90 days even when the org set no
                # policy. Require an explicit per-room opt-in (retention_enabled,
                # migration 049); otherwise the session falls back to the org-level
                # policy (org_days, default 0 = never). getattr keeps this safe if
                # the column predates the migration.
                if room and getattr(room, "retention_enabled", False):
                    retention_days = int(room.default_retention_days)
                else:
                    retention_days = org_days
                if retention_days <= 0:
                    continue
                age_from = session.ended_at or session.created_at
                if age_from and age_from.tzinfo is None:
                    age_from = age_from.replace(tzinfo=timezone.utc)
                if not age_from or age_from >= now - timedelta(days=retention_days):
                    continue
                canonical_id = session.session_id or str(session.id)
                if dry_run:
                    result["deleted"] += 1
                    continue
                try:
                    db.query(ChatHistory).filter(
                        ChatHistory.organization_id == session.organization_id,
                        ChatHistory.session_key == canonical_id,
                    ).delete(synchronize_session=False)
                    brigade_ref = _delete_session_record(db, session, canonical_id)
                    try:
                        from services.semantic_search_service import semantic_search
                        semantic_search.delete_session(canonical_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("retention qdrant purge failed session=%s: %s", canonical_id, exc)
                    if brigade_ref:
                        from services.brigade_writer import delete_session_from_brigade
                        asyncio.run(delete_session_from_brigade(*brigade_ref))
                    result["deleted"] += 1
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    result["errors"] += 1
                    logger.exception("retention purge failed session=%s: %s", canonical_id, exc)
                if result["deleted"] >= cap:
                    break
            if result["deleted"] >= cap:
                break
        return result
    finally:
        db.close()
