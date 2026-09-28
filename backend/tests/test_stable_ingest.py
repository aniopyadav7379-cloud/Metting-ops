from datetime import datetime, timedelta, timezone

import pytest

from database.database import SessionLocal
from database.models import RecordingSession, Transcription


def _pat_headers_for_org(client, org_slug: str):
    login = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert login.status_code == 200, login.text
    bearer = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created = client.post(
        "/api/auth/pats",
        headers=bearer,
        json={
            "name": "Stable production bridge",
            "scope": "stable.transcript.ingest",
            "organization_slug": org_slug,
            "expires_in_days": 90,
        },
    )
    assert created.status_code == 201, created.text
    return {
        "Authorization": f"Bearer {created.json()['plaintext']}",
        "X-MeetingOps-Org": org_slug,
        "Idempotency-Key": "stable-export-001",
    }


def _pat_headers(client):
    return _pat_headers_for_org(client, "magic-unicorn")


def _payload():
    return {
        "source": "unicorn-stable",
        "export_id": "stable-export-001",
        "exported_at": "2026-07-29T15:02:30Z",
        "room_id": "1b7da004-8c92-43ba-babb-ef77fe70385b",
        "room_name": "Executive Staff",
        "call_started_at": "2026-07-29T15:00:00Z",
        "call_ended_at": "2026-07-29T15:02:00Z",
        "label": "Stable call · Executive Staff",
        "line_count": 2,
        "lines": [
            {
                "participant_id": "aaron",
                "name": "Aaron",
                "text": "Ship the enterprise bridge.",
                "ts": 1785337205000,
                "is_final": True,
                "segment_id": "seg-a",
                "source": "local_mic",
                "identity_verification": "stable_room_member",
                "identity_verified": True,
                "content_verification": "client_caption",
            },
            {
                "participant_id": "shafen",
                "name": "Shafen",
                "text": "I will own verification.",
                "ts": 1785337210000,
                "is_final": True,
                "segment_id": "seg-b",
                "source": "agent",
                "identity_verification": "stable_known_agent",
                "identity_verified": True,
                "content_verification": "agent_generated_caption",
            },
        ],
        "exporter": {"username": "aaron"},
        "metadata": {"stable_version": "51f3888"},
    }


def test_ingest_requires_auth(client):
    response = client.post("/api/v1/sessions/ingest", json=_payload())
    assert response.status_code == 401


def test_ingest_rejects_jwt_even_for_authorized_user(client):
    login = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    response = client.post(
        "/api/v1/sessions/ingest",
        headers={
            "Authorization": f"Bearer {login.json()['access_token']}",
            "X-MeetingOps-Org": "magic-unicorn",
            "Idempotency-Key": "stable-export-001",
        },
        json=_payload(),
    )
    assert response.status_code == 401


def test_ingest_rejects_generic_unscoped_pat(client):
    login = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    created = client.post(
        "/api/auth/pats",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={"name": "Generic user PAT"},
    )
    response = client.post(
        "/api/v1/sessions/ingest",
        headers={
            "Authorization": f"Bearer {created.json()['plaintext']}",
            "X-MeetingOps-Org": "magic-unicorn",
            "Idempotency-Key": "stable-export-001",
        },
        json=_payload(),
    )
    assert response.status_code == 403


def test_integration_pat_cannot_authenticate_normal_api_or_mcp(client):
    headers = _pat_headers(client)
    bearer_only = {"Authorization": headers["Authorization"]}

    normal_api = client.get("/api/auth/me", headers=bearer_only)
    assert normal_api.status_code == 401

    mcp = client.post(
        "/mcp",
        headers={**bearer_only, "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert mcp.status_code == 401


def test_integration_pat_issuance_requires_org_admin(client):
    from auth.models import Organization, User, UserOrganization
    from auth.utils import get_password_hash

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "magic-unicorn").one()
        user = db.query(User).filter(User.username == "stable_non_admin").first()
        if user is None:
            user = User(
                email="stable-non-admin@example.test",
                username="stable_non_admin",
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                is_superuser=False,
            )
            db.add(user)
            db.flush()
            db.add(
                UserOrganization(
                    user_id=user.id,
                    organization_id=org.id,
                    role="user",
                )
            )
            db.commit()
    finally:
        db.close()

    login = client.post(
        "/api/auth/login",
        data={"username": "stable_non_admin", "password": "admin123"},
    )
    response = client.post(
        "/api/auth/pats",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={
            "name": "Forbidden bridge",
            "scope": "stable.transcript.ingest",
            "organization_slug": "magic-unicorn",
            "expires_in_days": 90,
        },
    )
    assert response.status_code == 403


def test_pat_rotation_rolls_back_replacement_and_revocation_together(client, monkeypatch):
    from auth.models import PersonalAccessToken, User
    from auth.pat import create_pat, rotate_pat_atomically

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").one()
        old, _ = create_pat(db, user=admin, name="Atomic rotation test")
        old_id = old.id

        def fail_commit():
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            rotate_pat_atomically(
                db,
                old=old,
                user=admin,
                expires_at=None,
            )
        db.rollback()
    finally:
        db.close()

    verify = SessionLocal()
    try:
        persisted_old = verify.get(PersonalAccessToken, old_id)
        assert persisted_old.revoked_at is None
        assert (
            verify.query(PersonalAccessToken)
            .filter(PersonalAccessToken.rotated_from_id == old_id)
            .count()
            == 0
        )
    finally:
        verify.close()


def test_expired_integration_pat_is_rejected(client):
    from auth.models import Organization, User
    from auth.pat import create_pat

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").one()
        org = db.query(Organization).filter(Organization.slug == "magic-unicorn").one()
        _, plaintext = create_pat(
            db,
            user=admin,
            name="Expired Stable bridge",
            scope="stable.transcript.ingest",
            organization_id=org.id,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    finally:
        db.close()

    response = client.post(
        "/api/v1/sessions/ingest",
        headers={
            "Authorization": f"Bearer {plaintext}",
            "X-MeetingOps-Org": "magic-unicorn",
            "Idempotency-Key": "stable-export-001",
        },
        json=_payload(),
    )
    assert response.status_code == 401


def test_ingest_requires_explicit_organization_header(client):
    headers = _pat_headers(client)
    headers.pop("X-MeetingOps-Org")
    response = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert response.status_code == 400


def test_ingest_rejects_unknown_organization_without_fallback(client):
    headers = _pat_headers(client)
    headers["X-MeetingOps-Org"] = "does-not-exist"
    response = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert response.status_code == 404


def test_ingest_rejects_inaccessible_organization(client):
    from auth.models import Organization

    db = SessionLocal()
    try:
        inaccessible = (
            db.query(Organization)
            .filter(Organization.slug == "stable-inaccessible")
            .first()
        )
        if inaccessible is None:
            db.add(
                Organization(
                    name="Stable Inaccessible",
                    slug="stable-inaccessible",
                    is_active=True,
                )
            )
            db.commit()
    finally:
        db.close()

    headers = _pat_headers(client)
    headers["X-MeetingOps-Org"] = "stable-inaccessible"
    response = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert response.status_code == 403


def test_ingest_creates_speaker_owned_session_and_is_idempotent(client):
    headers = _pat_headers(client)
    first = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    assert first.json()["session_url"].endswith(
        f"/sessions/{first.json()['session_id']}"
    )

    second = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["session_id"] == first.json()["session_id"]

    db = SessionLocal()
    try:
        rows = (
            db.query(RecordingSession)
            .filter(RecordingSession.external_id == "stable-export-001")
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.external_source == "unicorn-stable"
        assert row.organization_id is not None
        assert row.status == "completed"
        assert row.mode == "upload"
        assert row.source_type == "stable"
        assert row.speaker_count == 2
        assert row.transcript_diarized["speakers"] == ["Aaron", "Shafen"]
        assert [s["speaker"] for s in row.transcript_diarized["segments"]] == [
            "Aaron",
            "Shafen",
        ]
        assert [s["source"] for s in row.transcript_diarized["segments"]] == [
            "local_mic",
            "agent",
        ]
        assert row.participants == [
            {"id": "aaron", "name": "Aaron", "role": "participant", "kind": "human"},
            {"id": "shafen", "name": "Shafen", "role": "agent", "kind": "agent"},
        ]
        transcript_rows = (
            db.query(Transcription)
            .filter(Transcription.session_id == row.id)
            .order_by(Transcription.id)
            .all()
        )
        assert [item.speaker for item in transcript_rows] == ["Aaron", "Shafen"]
        assert [item.confidence for item in transcript_rows] == [None, None]
        assert row.processing_metadata["federation"]["verification"] == {
            "speaker_identity": "stable_server_authorized",
            "transcript_text": "unverified",
            "acoustic_confidence": "not_provided",
        }
        assert row.processing_metadata["federation"]["ingest_principal"]["pat_id"]
        assert [
            segment["content_verification"]
            for segment in row.transcript_diarized["segments"]
        ] == ["client_caption", "agent_generated_caption"]
    finally:
        db.close()


def test_same_export_id_is_independent_across_organizations(client):
    from auth.models import Organization, User, UserOrganization

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").one()
        second_org = (
            db.query(Organization)
            .filter(Organization.slug == "stable-second-org")
            .first()
        )
        if second_org is None:
            second_org = Organization(
                name="Stable Second Org",
                slug="stable-second-org",
                is_active=True,
            )
            db.add(second_org)
            db.flush()
        membership = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == admin.id,
                UserOrganization.organization_id == second_org.id,
            )
            .first()
        )
        if membership is None:
            db.add(
                UserOrganization(
                    user_id=admin.id,
                    organization_id=second_org.id,
                    role="admin",
                )
            )
        db.commit()
        second_org_id = second_org.id
    finally:
        db.close()

    body = _payload()
    body["export_id"] = "stable-cross-org-export"
    default_headers = _pat_headers(client)
    default_headers["Idempotency-Key"] = body["export_id"]
    first = client.post(
        "/api/v1/sessions/ingest",
        headers=default_headers,
        json=body,
    )
    assert first.status_code == 200, first.text

    second_headers = _pat_headers_for_org(client, "stable-second-org")
    second_headers["Idempotency-Key"] = body["export_id"]
    second = client.post(
        "/api/v1/sessions/ingest",
        headers=second_headers,
        json=body,
    )
    assert second.status_code == 200, second.text
    assert second.json()["session_id"] != first.json()["session_id"]

    db = SessionLocal()
    try:
        rows = (
            db.query(RecordingSession)
            .filter(RecordingSession.external_id == body["export_id"])
            .all()
        )
        assert len(rows) == 2
        from auth.models import Organization

        default_org_id = (
            db.query(Organization)
            .filter(Organization.slug == "magic-unicorn")
            .one()
            .id
        )
        assert {row.organization_id for row in rows} == {
            default_org_id,
            second_org_id,
        }
    finally:
        db.close()


def test_ingest_rejects_mismatched_idempotency_key(client):
    headers = _pat_headers(client)
    headers["Idempotency-Key"] = "different"
    response = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert response.status_code == 409


def test_ingest_rejects_same_export_id_with_changed_payload(client):
    headers = _pat_headers(client)
    first = client.post("/api/v1/sessions/ingest", headers=headers, json=_payload())
    assert first.status_code == 200, first.text

    changed = _payload()
    changed["lines"][0]["text"] = "Tampered replacement."
    second = client.post("/api/v1/sessions/ingest", headers=headers, json=changed)
    assert second.status_code == 409
