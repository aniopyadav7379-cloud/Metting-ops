from __future__ import annotations

import uuid


def _models():
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import RecordingSession, SpeakerProfile, SpeakerSessionLink

    return Organization, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink


def _seed_session_with_link(
    db,
    *,
    contact_id: str | None,
    confirmed: bool,
    similarity: float,
):
    Organization, _, RecordingSession, SpeakerProfile, SpeakerSessionLink = _models()
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"Speaker Contact {suffix}", slug=f"speaker-contact-{suffix}", is_active=True)
    db.add(org)
    db.flush()
    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        title="Speaker contact stamp test",
        name="Speaker contact stamp test",
        status="completed",
        organization_id=org.id,
        participants=[],
    )
    db.add(session)
    db.flush()
    speaker = SpeakerProfile(
        organization_id=org.id,
        display_name="Devon Clarke",
        email="devon.clarke@clarkeadvisory.com",
        contact_id=contact_id,
        contact_link_confirmed=confirmed,
    )
    db.add(speaker)
    db.flush()
    link = SpeakerSessionLink(
        session_id=session.id,
        organization_id=org.id,
        raw_label="SPEAKER_00",
        speaker_id=speaker.id,
        similarity=similarity,
        source="auto",
        confirmed=False,
    )
    db.add(link)
    db.commit()
    db.refresh(session)
    return session


def test_confirmed_high_confidence_speaker_contact_stamps_participant(app):
    _, SessionLocal, RecordingSession, _, _ = _models()
    from services.speaker_service import stamp_confirmed_speaker_contacts

    db = SessionLocal()
    try:
        session = _seed_session_with_link(
            db,
            contact_id="e799a820-0a69-4fa6-820b-5f2911833bc8",
            confirmed=True,
            similarity=0.91,
        )
        summary = stamp_confirmed_speaker_contacts(session, db, threshold=0.80)
        db.refresh(session)
        assert summary["stamped"] == 1
        assert session.participants == [
            {
                "id": session.participants[0]["id"],
                "name": "Devon Clarke",
                "email": "devon.clarke@clarkeadvisory.com",
                "role": "speaker",
                "contact_id": "e799a820-0a69-4fa6-820b-5f2911833bc8",
            }
        ]

        # Idempotent on contact_id.
        summary = stamp_confirmed_speaker_contacts(session, db, threshold=0.80)
        db.refresh(session)
        assert summary["stamped"] == 0
        assert len(session.participants) == 1
    finally:
        db.close()


def test_unconfirmed_or_low_confidence_contact_is_suggestion_not_stamp(app):
    _, SessionLocal, RecordingSession, _, _ = _models()
    from services.speaker_service import stamp_confirmed_speaker_contacts

    db = SessionLocal()
    try:
        session = _seed_session_with_link(
            db,
            contact_id="e799a820-0a69-4fa6-820b-5f2911833bc8",
            confirmed=False,
            similarity=0.91,
        )
        summary = stamp_confirmed_speaker_contacts(session, db, threshold=0.80)
        db.refresh(session)
        assert summary["stamped"] == 0
        assert summary["suggested"] == 1
        assert session.participants == []
        assert session.processing_metadata["speaker_contact_suggestions"][0]["reason"] == "contact_link_unconfirmed"

        low = _seed_session_with_link(
            db,
            contact_id="384e4beb-4c2b-43ae-80c0-2f8f03317fde",
            confirmed=True,
            similarity=0.72,
        )
        summary = stamp_confirmed_speaker_contacts(low, db, threshold=0.80)
        db.refresh(low)
        assert summary["stamped"] == 0
        assert low.participants == []
        assert low.processing_metadata["speaker_contact_suggestions"][0]["reason"] == "below_threshold"
    finally:
        db.close()
