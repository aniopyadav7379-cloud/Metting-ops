from datetime import datetime, timedelta, timezone
import uuid


def test_retention_is_off_by_default(monkeypatch):
    from services.data_retention import run_data_retention_purge

    monkeypatch.delenv("MEETING_RETENTION_ENABLED", raising=False)
    assert run_data_retention_purge() == {"enabled": False, "deleted": 0}


def test_retention_deletes_only_expired_completed_sessions(app, monkeypatch):
    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import RecordingSession
    from services.data_retention import run_data_retention_purge
    from services.semantic_search_service import semantic_search

    monkeypatch.setenv("MEETING_RETENTION_ENABLED", "true")
    monkeypatch.setenv("MEETING_RETENTION_DAYS", "30")
    monkeypatch.setattr(semantic_search, "delete_session", lambda session_id: True)
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "magic-unicorn").first()
        org.settings = {**(org.settings or {}), "retention_days": 30}
        user = db.query(User).filter(User.username == "admin").first()
        old = RecordingSession(
            session_id=str(uuid.uuid4()), name="expired", status="completed",
            organization_id=org.id, user_id=user.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        recent = RecordingSession(
            session_id=str(uuid.uuid4()), name="recent", status="completed",
            organization_id=org.id, user_id=user.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db.add_all([old, recent])
        db.commit()
        old_id, recent_id = old.id, recent.id
    finally:
        db.close()

    result = run_data_retention_purge()
    assert result["deleted"] >= 1
    db = SessionLocal()
    try:
        assert db.get(RecordingSession, old_id) is None
        assert db.get(RecordingSession, recent_id) is not None
    finally:
        db.close()


def test_room_without_optin_does_not_purge_at_implicit_90(app, monkeypatch):
    """Retention landmine fix: a room-attached session must NOT be purged by the
    room's default_retention_days (server_default 90) unless the room explicitly
    opts in. Retention is ENABLED globally, the org has NO policy, and the room
    is opt-out (default) — a 400-day-old session must survive."""
    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import RecordingSession
    from database.models_rooms import ConferenceRoom
    from services.data_retention import run_data_retention_purge
    from services.semantic_search_service import semantic_search

    monkeypatch.setenv("MEETING_RETENTION_ENABLED", "true")
    monkeypatch.delenv("MEETING_RETENTION_DAYS", raising=False)  # env default => 0 (never)
    monkeypatch.setattr(semantic_search, "delete_session", lambda session_id: True)

    db = SessionLocal()
    try:
        org = Organization(
            name="retn-optout", slug=f"retn-optout-{uuid.uuid4().hex[:6]}", is_active=True
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        # Deliberately NO org-level retention policy → org_days == 0 (never).
        user = db.query(User).filter(User.username == "admin").first()
        room = ConferenceRoom(
            organization_id=org.id,
            name=f"room-{uuid.uuid4().hex[:6]}",
            status="idle",
            # default_retention_days defaults to 90, retention_enabled defaults to False
        )
        db.add(room)
        db.commit()
        db.refresh(room)
        ancient = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="ancient-room-session",
            status="completed",
            organization_id=org.id,
            user_id=user.id,
            room_id=room.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=400),
        )
        db.add(ancient)
        db.commit()
        ancient_id = ancient.id
    finally:
        db.close()

    run_data_retention_purge()

    db = SessionLocal()
    try:
        assert db.get(RecordingSession, ancient_id) is not None, (
            "room-attached session was purged at the implicit 90-day default "
            "without an explicit per-room opt-in (landmine regression)"
        )
    finally:
        db.close()


def test_room_with_optin_purges_expired(app, monkeypatch):
    """When a room explicitly opts in (retention_enabled=True), its
    default_retention_days drives the purge as designed."""
    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import RecordingSession
    from database.models_rooms import ConferenceRoom
    from services.data_retention import run_data_retention_purge
    from services.semantic_search_service import semantic_search

    monkeypatch.setenv("MEETING_RETENTION_ENABLED", "true")
    monkeypatch.delenv("MEETING_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(semantic_search, "delete_session", lambda session_id: True)

    db = SessionLocal()
    try:
        org = Organization(
            name="retn-optin", slug=f"retn-optin-{uuid.uuid4().hex[:6]}", is_active=True
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        user = db.query(User).filter(User.username == "admin").first()
        room = ConferenceRoom(
            organization_id=org.id,
            name=f"room-{uuid.uuid4().hex[:6]}",
            status="idle",
            default_retention_days=30,
            retention_enabled=True,
        )
        db.add(room)
        db.commit()
        db.refresh(room)
        expired = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="expired-room-session",
            status="completed",
            organization_id=org.id,
            user_id=user.id,
            room_id=room.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        db.add(expired)
        db.commit()
        expired_id = expired.id
    finally:
        db.close()

    run_data_retention_purge()

    db = SessionLocal()
    try:
        assert db.get(RecordingSession, expired_id) is None, (
            "room opted into retention (30d) but a 45-day-old session was not purged"
        )
    finally:
        db.close()
