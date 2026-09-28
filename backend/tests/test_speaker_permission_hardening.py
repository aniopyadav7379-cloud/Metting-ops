"""Behavior-level guards for speaker biometrics and share-management data."""
from __future__ import annotations

import uuid

from auth.utils import get_password_hash
from services.invitation_tokens import hash_invitation_secret


def _seed_workspace():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        RecordingSession,
        SessionCollaborator,
        SpeakerProfile,
        SpeakerSessionLink,
    )

    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        org = Organization(
            name=f"Permission Hardening {suffix}",
            slug=f"permission-hardening-{suffix}",
            is_active=True,
        )
        creator = User(
            email=f"creator-{suffix}@example.com",
            username=f"creator_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
            tier="pro",
        )
        viewer = User(
            email=f"viewer-{suffix}@example.com",
            username=f"viewer_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
            tier="pro",
        )
        db.add_all([org, creator, viewer])
        db.flush()
        db.add_all([
            UserOrganization(
                user_id=creator.id,
                organization_id=org.id,
                role="admin",
            ),
            UserOrganization(
                user_id=viewer.id,
                organization_id=org.id,
                role="viewer",
            ),
        ])
        session = RecordingSession(
            session_id=f"permission-session-{suffix}",
            title="Permission hardening",
            status="completed",
            user_id=creator.id,
            organization_id=org.id,
            transcript_diarized={
                "segments": [
                    {
                        "speaker": "SPEAKER_00",
                        "raw_label": "SPEAKER_00",
                        "start": 0,
                        "end": 4,
                        "text": "Representative meeting line.",
                        "embedding": [0.1, 0.2],
                    }
                ],
                "speaker_turns": [
                    {
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 4,
                        "embedding": [0.1, 0.2],
                    }
                ],
            },
        )
        db.add(session)
        db.flush()
        speaker = SpeakerProfile(
            organization_id=org.id,
            display_name=f"Known Speaker {suffix}",
        )
        db.add(speaker)
        db.flush()
        link = SpeakerSessionLink(
            session_id=session.id,
            organization_id=org.id,
            raw_label="SPEAKER_00",
        )
        db.add(link)
        invite = SessionCollaborator(
            session_id=session.id,
            email=f"invitee-{suffix}@example.com",
            access_level="read",
            invited_by_user_id=creator.id,
            token=uuid.uuid4(),
            token_hash=hash_invitation_secret(f"seed-{suffix}"),
            token_version=2,
            delivery_state="sent",
        )
        db.add(invite)
        db.commit()
        return {
            "slug": org.slug,
            "creator_username": creator.username,
            "viewer_username": viewer.username,
            "session_key": session.session_id,
            "session_id": session.id,
            "link_id": link.id,
            "speaker_id": speaker.id,
        }
    finally:
        db.close()


def _headers(client, username: str, slug: str):
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": "admin123"},
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-MeetingOps-Org": slug,
    }


def _all_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def test_viewer_can_read_links_but_cannot_mutate_speaker_identity(client):
    seeded = _seed_workspace()
    viewer_headers = _headers(
        client, seeded["viewer_username"], seeded["slug"]
    )

    read = client.get(
        f"/api/sessions/{seeded['session_key']}/speaker-links",
        headers=viewer_headers,
    )
    assert read.status_code == 200, read.text

    session_read = client.get(
        f"/api/simple/recording-sessions/{seeded['session_key']}",
        headers=viewer_headers,
    )
    assert session_read.status_code == 200, session_read.text
    response_keys = set(_all_keys(session_read.json()))
    assert "embedding" not in response_keys
    assert "embeddings" not in response_keys
    assert "centroid_embedding" not in response_keys
    assert "speaker_turns" not in response_keys

    mutation = client.patch(
        (
            f"/api/sessions/{seeded['session_key']}/speaker-links/"
            f"{seeded['link_id']}"
        ),
        headers=viewer_headers,
        json={"speaker_id": seeded["speaker_id"], "confirmed": True},
    )
    # The tier/feature gate may reject first (403); the session access gate
    # otherwise uses non-disclosing 404. Either way, no biometric mutation.
    assert mutation.status_code in {403, 404}

    candidates = client.get(
        f"/api/speakers?session_id={seeded['session_key']}",
        headers=viewer_headers,
    )
    assert candidates.status_code == 404


def test_collaborator_roster_and_invite_tokens_are_manager_only(client):
    seeded = _seed_workspace()
    creator_headers = _headers(
        client, seeded["creator_username"], seeded["slug"]
    )
    viewer_headers = _headers(
        client, seeded["viewer_username"], seeded["slug"]
    )
    url = (
        f"/api/simple/recording-sessions/{seeded['session_key']}/permissions"
    )

    creator_read = client.get(url, headers=creator_headers)
    assert creator_read.status_code == 200, creator_read.text
    creator_keys = set(_all_keys(creator_read.json()))
    assert "token" not in creator_keys
    assert "token_hash" not in creator_keys
    assert "invite_url" not in creator_keys

    viewer_read = client.get(url, headers=viewer_headers)
    assert viewer_read.status_code == 403
    assert "token" not in viewer_read.text.lower()
