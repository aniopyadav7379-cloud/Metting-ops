"""Endpoint tests for self-intro name suggestions on speaker-link reads.

Covers the on-read enrichment contract:
  * GET /api/speakers/unassigned-links surfaces name_suggestions on null links
    while the existing top_matches/preview assertions still hold (additive).
  * The PRIVACY/attribution invariant: an intro spoken by SPEAKER_01 is NEVER
    attributed to SPEAKER_00's unassigned row (own-utterances-only).
  * GET /api/sessions/{id}/speaker-links surfaces suggestions on the unnamed
    link and [] on the assigned (voice-matched) link.

Mirrors the seed + login style of test_speakers_unassigned_and_merge.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from auth.utils import get_password_hash
from services.speaker_service import encode_embedding


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    )

    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    )


def _seed_user_org(*, slug: str, username: str, role: str = "admin"):
    Organization, User, UserOrganization, SessionLocal, _, _, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                tier="pro",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        membership = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        )
        if not membership:
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role=role))
        else:
            membership.role = role
        db.commit()
        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _login_headers(client, username: str, org_slug: str):
    resp = client.post("/api/auth/login", data={"username": username, "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-MeetingOps-Org": org_slug,
    }


def _seed_session(
    *,
    org_id: int,
    user_id: int,
    speaker00_texts: list[str],
    speaker01_texts: list[str],
    title: str = "Self-Intro Session",
):
    """Seed a completed session: SPEAKER_00 stays UNIDENTIFIED (null link),
    SPEAKER_01 is voice-matched to an assigned speaker. Each list is that
    cluster's OWN segment texts."""
    (
        _,
        _,
        _,
        SessionLocal,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    ) = _models()
    db = SessionLocal()
    try:
        segments = []
        idx = 0
        for txt in speaker00_texts:
            segments.append({
                "idx": idx, "speaker": "SPEAKER_00", "raw_label": "SPEAKER_00",
                "start": float(idx), "end": float(idx) + 1.0, "text": txt,
                "embedding": [1.0, 0.0],
            })
            idx += 1
        for txt in speaker01_texts:
            segments.append({
                "idx": idx, "speaker": "SPEAKER_01", "raw_label": "SPEAKER_01",
                "start": float(idx), "end": float(idx) + 1.0, "text": txt,
                "embedding": [0.0, 1.0],
            })
            idx += 1

        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            title=title,
            name=title,
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            started_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            transcript_diarized={
                "segments": segments,
                "speakers": ["SPEAKER_00", "SPEAKER_01"],
            },
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        assigned_speaker = SpeakerProfile(
            organization_id=org_id,
            display_name=f"Assigned {uuid.uuid4().hex[:6]}",
            centroid_embedding=encode_embedding([0.0, 1.0]),
            embedding_dim=2,
            embedding_model="test-model",
            sample_count=1,
        )
        db.add(assigned_speaker)
        db.flush()

        unassigned = SpeakerSessionLink(
            session_id=session.id, organization_id=org_id,
            raw_label="SPEAKER_00", speaker_id=None,
        )
        assigned = SpeakerSessionLink(
            session_id=session.id, organization_id=org_id,
            raw_label="SPEAKER_01", speaker_id=assigned_speaker.id,
            source="auto", confirmed=False, similarity=0.91,
        )
        db.add_all([unassigned, assigned])
        db.commit()
        db.refresh(session)
        db.refresh(unassigned)
        db.refresh(assigned)
        return session, unassigned, assigned
    finally:
        db.close()


def test_unassigned_links_surface_self_intro_suggestion(client):
    org, user = _seed_user_org(slug="self-intro-unassigned", username="self_intro_unassigned")
    session, link, _assigned = _seed_session(
        org_id=org.id,
        user_id=user.id,
        speaker00_texts=[
            "Hi everyone, I'm John and I'll be leading the review today.",
            "Let's get started with the budget.",
        ],
        speaker01_texts=["Sounds good to me."],
    )

    resp = client.get(
        "/api/speakers/unassigned-links",
        headers=_login_headers(client, "self_intro_unassigned", org.slug),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only the SPEAKER_00 null link is unassigned.
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == link.id
    assert item["raw_label"] == "SPEAKER_00"
    # Additive: the existing preview field is unchanged.
    assert item["preview"].startswith("Hi everyone")
    # The new field carries the extracted name + auditable evidence.
    names = [s["name"] for s in item["name_suggestions"]]
    assert names == ["John"]
    assert "John" in item["name_suggestions"][0]["evidence"]


def test_unassigned_links_does_not_attribute_other_speakers_intro(client):
    """LOAD-BEARING PRIVACY TEST: SPEAKER_00 (unidentified) says no intro;
    SPEAKER_01 says "Hi, I'm John". SPEAKER_00's row must surface NO name —
    we only scan a cluster's OWN utterances."""
    org, user = _seed_user_org(slug="self-intro-privacy", username="self_intro_privacy")
    _session, link, _assigned = _seed_session(
        org_id=org.id,
        user_id=user.id,
        speaker00_texts=[
            "Okay so where are we on the timeline?",
            "And the budget for next quarter?",
        ],
        speaker01_texts=["Hi, I'm John, I can answer that."],
    )

    resp = client.get(
        "/api/speakers/unassigned-links",
        headers=_login_headers(client, "self_intro_privacy", org.slug),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == link.id
    assert item["raw_label"] == "SPEAKER_00"
    # The intro belongs to SPEAKER_01 and must NOT leak onto SPEAKER_00.
    assert item["name_suggestions"] == []


def test_per_session_links_suggest_on_null_and_empty_on_assigned(client):
    org, user = _seed_user_org(slug="self-intro-session", username="self_intro_session")
    session, unassigned, assigned = _seed_session(
        org_id=org.id,
        user_id=user.id,
        speaker00_texts=["This is Maria, calling about the contract."],
        # The assigned (voice-matched) speaker also self-introduces, but a
        # matched link must surface NO suggestions.
        speaker01_texts=["My name is Robert Lang, I run engineering."],
    )

    resp = client.get(
        f"/api/sessions/{session.session_id}/speaker-links",
        headers=_login_headers(client, "self_intro_session", org.slug),
    )
    assert resp.status_code == 200, resp.text
    rows = {r["raw_label"]: r for r in resp.json()}

    # Unidentified link -> suggestion from its OWN line.
    assert rows["SPEAKER_00"]["speaker_id"] is None
    assert [s["name"] for s in rows["SPEAKER_00"]["name_suggestions"]] == ["Maria"]

    # Voice-matched link -> assigned speaker, NO suggestions even though that
    # cluster contains a self-intro.
    assert rows["SPEAKER_01"]["speaker_id"] == assigned.speaker_id
    assert rows["SPEAKER_01"]["name_suggestions"] == []


def test_per_session_links_no_intro_yields_empty_suggestions(client):
    """A null link whose own lines contain no intro returns [] (not absent)."""
    org, user = _seed_user_org(slug="self-intro-none", username="self_intro_none")
    session, _unassigned, _assigned = _seed_session(
        org_id=org.id,
        user_id=user.id,
        speaker00_texts=["I'm happy to help, let's dig in.", "Where did we leave off?"],
        speaker01_texts=["Right, the roadmap."],
    )

    resp = client.get(
        f"/api/sessions/{session.session_id}/speaker-links",
        headers=_login_headers(client, "self_intro_none", org.slug),
    )
    assert resp.status_code == 200, resp.text
    rows = {r["raw_label"]: r for r in resp.json()}
    # "I'm happy" is NOT a name -> empty, field present.
    assert rows["SPEAKER_00"]["speaker_id"] is None
    assert rows["SPEAKER_00"]["name_suggestions"] == []
