from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from auth.utils import get_password_hash
from services.speaker_service import encode_embedding


def _models():
    from auth.models import AuditLog, Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
        SpeakerVoiceSample,
    )

    return (
        AuditLog,
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
        SpeakerVoiceSample,
    )


def _seed_user_org(*, slug: str, username: str, role: str = "admin"):
    _, Organization, User, UserOrganization, SessionLocal, _, _, _, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True, plan="pro")  # billing-1: paid workspace matches the pro user
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


def _seed_speaker(*, org_id: int, name: str, centroid: list[float], sample_count: int = 1):
    _, _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, SpeakerVoiceSample = _models()
    db = SessionLocal()
    try:
        speaker = SpeakerProfile(
            organization_id=org_id,
            display_name=name,
            centroid_embedding=encode_embedding(centroid),
            embedding_dim=len(centroid),
            embedding_model="test-model",
            sample_count=sample_count,
        )
        db.add(speaker)
        db.commit()
        db.refresh(speaker)
        return speaker
    finally:
        db.close()


def _seed_session_with_links(*, org_id: int, user_id: int, title: str = "Speaker Session"):
    _, _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, _ = _models()
    db = SessionLocal()
    try:
        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            title=title,
            name=title,
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            started_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            transcript_diarized={
                "segments": [
                    {
                        "idx": 0,
                        "speaker": "SPEAKER_00",
                        "raw_label": "SPEAKER_00",
                        "start": 0.0,
                        "end": 3.0,
                        "text": "Aaron talked about the launch sequence and budget.",
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "idx": 1,
                        "speaker": "SPEAKER_00",
                        "raw_label": "SPEAKER_00",
                        "start": 4.0,
                        "end": 7.0,
                        "text": "More details from the same voice.",
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "idx": 2,
                        "speaker": "SPEAKER_01",
                        "raw_label": "SPEAKER_01",
                        "start": 7.0,
                        "end": 9.0,
                        "text": "Assigned voice.",
                        "embedding": [0.0, 1.0],
                    },
                ],
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
            session_id=session.id,
            organization_id=org_id,
            raw_label="SPEAKER_00",
            speaker_id=None,
        )
        assigned = SpeakerSessionLink(
            session_id=session.id,
            organization_id=org_id,
            raw_label="SPEAKER_01",
            speaker_id=assigned_speaker.id,
        )
        db.add_all([unassigned, assigned])
        db.commit()
        db.refresh(session)
        db.refresh(unassigned)
        return session, unassigned
    finally:
        db.close()


def test_unassigned_links_returns_only_null_links_in_active_org_and_orders_matches(client):
    org, user = _seed_user_org(slug="speaker-unassigned", username="speaker_unassigned")
    other_org, other_user = _seed_user_org(slug="speaker-unassigned-other", username="speaker_unassigned_other")
    _seed_speaker(org_id=org.id, name="Aaron Stransky", centroid=[1.0, 0.0])
    _seed_speaker(org_id=org.id, name="Shafen Khan", centroid=[0.4, 0.6])
    _seed_speaker(org_id=other_org.id, name="Other Org Speaker", centroid=[1.0, 0.0])
    session, link = _seed_session_with_links(org_id=org.id, user_id=user.id)
    _seed_session_with_links(org_id=other_org.id, user_id=other_user.id, title="Other Org Session")

    resp = client.get(
        "/api/speakers/unassigned-links",
        headers=_login_headers(client, "speaker_unassigned", org.slug),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == link.id
    assert item["raw_label"] == "SPEAKER_00"
    assert item["session_id"] == session.session_id
    assert item["session_title"] == "Speaker Session"
    assert item["duration_seconds"] == 6.0
    assert item["segment_count"] == 2
    assert item["preview"].startswith("Aaron talked")
    assert [m["display_name"] for m in item["top_matches"]][:2] == [
        "Aaron Stransky",
        "Shafen Khan",
    ]
    assert item["top_matches"][0]["similarity"] >= item["top_matches"][1]["similarity"]


def test_unassigned_links_empty_when_none_unassigned(client):
    org, user = _seed_user_org(slug="speaker-unassigned-empty", username="speaker_empty")
    resp = client.get(
        "/api/speakers/unassigned-links",
        headers=_login_headers(client, "speaker_empty", org.slug),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


def _seed_merge_fixture(*, org_id: int, user_id: int):
    _, _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, SpeakerVoiceSample = _models()
    db = SessionLocal()
    try:
        target = SpeakerProfile(
            organization_id=org_id,
            display_name=f"Target {uuid.uuid4().hex[:6]}",
            centroid_embedding=encode_embedding([1.0, 0.0]),
            embedding_dim=2,
            embedding_model="test-model",
            sample_count=1,
        )
        source = SpeakerProfile(
            organization_id=org_id,
            display_name=f"Source {uuid.uuid4().hex[:6]}",
            centroid_embedding=encode_embedding([0.0, 1.0]),
            embedding_dim=2,
            embedding_model="test-model",
            sample_count=2,
        )
        db.add_all([target, source])
        db.flush()
        db.add(SpeakerVoiceSample(
            speaker_id=target.id,
            organization_id=org_id,
            source="enrollment",
            embedding=encode_embedding([1.0, 0.0]),
            embedding_dim=2,
            embedding_model="test-model",
        ))
        db.add_all([
            SpeakerVoiceSample(
                speaker_id=source.id,
                organization_id=org_id,
                source="enrollment",
                embedding=encode_embedding([0.0, 1.0]),
                embedding_dim=2,
                embedding_model="test-model",
            ),
            SpeakerVoiceSample(
                speaker_id=source.id,
                organization_id=org_id,
                source="enrollment",
                embedding=encode_embedding([0.0, 1.0]),
                embedding_dim=2,
                embedding_model="test-model",
            ),
        ])
        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            title="Merge Session",
            name="Merge Session",
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            transcript_diarized={"segments": []},
        )
        db.add(session)
        db.flush()
        db.add_all([
            SpeakerSessionLink(
                session_id=session.id,
                organization_id=org_id,
                raw_label="SPEAKER_00",
                speaker_id=source.id,
            ),
            SpeakerSessionLink(
                session_id=session.id,
                organization_id=org_id,
                raw_label="SPEAKER_01",
                speaker_id=target.id,
            ),
        ])
        db.commit()
        db.refresh(target)
        db.refresh(source)
        return target.id, source.id, source.display_name
    finally:
        db.close()


def test_merge_moves_samples_links_deletes_source_and_writes_audit(client):
    AuditLog, _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, SpeakerVoiceSample = _models()
    org, user = _seed_user_org(slug="speaker-merge", username="speaker_merge")
    target_id, source_id, source_name = _seed_merge_fixture(org_id=org.id, user_id=user.id)

    resp = client.post(
        f"/api/speakers/{target_id}/merge",
        headers=_login_headers(client, "speaker_merge", org.slug),
        json={"source_id": source_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == target_id
    assert body["sample_count"] == 3

    db = SessionLocal()
    try:
        assert db.query(SpeakerProfile).filter(SpeakerProfile.id == source_id).first() is None
        assert db.query(SpeakerVoiceSample).filter(SpeakerVoiceSample.speaker_id == source_id).count() == 0
        assert db.query(SpeakerVoiceSample).filter(SpeakerVoiceSample.speaker_id == target_id).count() == 3
        assert db.query(SpeakerSessionLink).filter(SpeakerSessionLink.speaker_id == source_id).count() == 0
        assert db.query(SpeakerSessionLink).filter(SpeakerSessionLink.speaker_id == target_id).count() == 2
        audit = (
            db.query(AuditLog)
            .filter(AuditLog.action == "speaker_merge", AuditLog.resource_id == str(target_id))
            .first()
        )
        assert audit is not None
        assert audit.details == {
            "merged_from": source_id,
            "source_name": source_name,
            "samples_moved": 2,
            "links_moved": 1,
        }
    finally:
        db.close()


def test_merge_refuses_self_and_cross_org(client):
    org, user = _seed_user_org(slug="speaker-merge-refuse", username="speaker_merge_refuse")
    other_org, other_user = _seed_user_org(slug="speaker-merge-refuse-other", username="speaker_merge_other")
    target_id, source_id, _ = _seed_merge_fixture(org_id=org.id, user_id=user.id)
    other_target_id, _, _ = _seed_merge_fixture(org_id=other_org.id, user_id=other_user.id)
    headers = _login_headers(client, "speaker_merge_refuse", org.slug)

    same = client.post(f"/api/speakers/{target_id}/merge", headers=headers, json={"source_id": target_id})
    assert same.status_code == 400

    cross = client.post(f"/api/speakers/{target_id}/merge", headers=headers, json={"source_id": other_target_id})
    assert cross.status_code == 404


def test_merge_rolls_back_on_error(client, monkeypatch):
    _, _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, SpeakerVoiceSample = _models()
    org, user = _seed_user_org(slug="speaker-merge-rollback", username="speaker_merge_rollback")
    target_id, source_id, _ = _seed_merge_fixture(org_id=org.id, user_id=user.id)

    import api.speakers as speakers_api

    def explode(db, source):
        raise RuntimeError("forced merge failure")

    monkeypatch.setattr(speakers_api, "_delete_merged_source", explode)
    resp = client.post(
        f"/api/speakers/{target_id}/merge",
        headers=_login_headers(client, "speaker_merge_rollback", org.slug),
        json={"source_id": source_id},
    )
    assert resp.status_code == 500

    db = SessionLocal()
    try:
        assert db.query(SpeakerProfile).filter(SpeakerProfile.id == source_id).first() is not None
        assert db.query(SpeakerVoiceSample).filter(SpeakerVoiceSample.speaker_id == source_id).count() == 2
        assert db.query(SpeakerSessionLink).filter(SpeakerSessionLink.speaker_id == source_id).count() == 1
    finally:
        db.close()
