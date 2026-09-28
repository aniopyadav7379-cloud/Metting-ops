"""Tests for the additional_recipients (free-form external email) feature
on POST /api/simple/recording-sessions/{id}/email-attendees.

These pin the contract that Aaron asked for: "Can I email this and a link to
people not named as attending this meeting?"

Coverage:
  - Sending with ONLY additional_recipients (no speaker_ids) creates a
    SessionCollaborator row per email + a magic-link token + drives one
    _postmark_send call per address.
  - Combined speaker_ids + additional_recipients sends to both groups (no
    dedupe collisions).
  - Invalid email address in additional_recipients -> 422 from pydantic.
  - Empty speaker_ids + empty additional_recipients on a session with no
    participants -> 400 (no-recipients error).
  - Cross-org safety: the magic-link token grants access to ONLY the one
    session it was minted for. A token from session A in org-A resolves
    to session A and nothing else. The token in org A cannot return data
    from a session in org B.
"""
from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import unquote
from unittest.mock import patch

import pytest

from auth.utils import get_password_hash


@pytest.fixture(autouse=True)
def _enabled_test_invitation_transport(monkeypatch):
    monkeypatch.setenv("MEETING_INVITE_V2_ISSUANCE_ENABLED", "true")
    monkeypatch.setenv(
        "MEETING_OPS_PUBLIC_URL",
        "https://meetingops.test",
    )
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession, SessionCollaborator

    return Organization, User, UserOrganization, SessionLocal, RecordingSession, SessionCollaborator


def _seed_user(db, username: str, password: str, email: str, *, is_superuser: bool = False):
    _, User, _, _, _, _ = _models()
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        is_active=True,
        is_verified=True,
        is_superuser=is_superuser,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_session(db, *, organization_id: int, user_id: int, title: str):
    _, _, _, _, RecordingSession, _ = _models()
    sess = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        status="completed",
        organization_id=organization_id,
        user_id=user_id,
        transcript_simple="hello world",
        transcript="hello world",
        summary="short summary",
        final_summary={"executive": "short summary"},
        duration=60.0,
        participants=[],
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@pytest.fixture()
def two_orgs_email(client):
    """Two orgs with one session each + a superuser caller in org A.

    Returns headers + DB ids for the two sessions so cross-org safety
    checks can drive the resolve-access-token endpoint with the right
    inputs.
    """
    Organization, _, UserOrganization, SessionLocal, _, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]

    try:
        org_a = Organization(name=f"Org Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True)
        org_b = Organization(name=f"Org Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True)
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        # Superuser caller — bypasses the admin/manager/session-creator gate
        # so we can focus on the additional_recipients contract here.
        caller = _seed_user(
            db,
            f"caller_{suffix}",
            "Password123",
            f"caller_{suffix}@example.com",
            is_superuser=True,
        )
        # Make the caller a member of org A so the X-MeetingOps-Org header
        # resolves cleanly.
        db.add(UserOrganization(user_id=caller.id, organization_id=org_a.id, role="admin"))
        db.commit()

        sess_a = _make_session(db, organization_id=org_a.id, user_id=caller.id, title="Alpha planning")
        sess_b = _make_session(db, organization_id=org_b.id, user_id=caller.id, title="Bravo deepdive")

        ctx = {
            "org_a_slug": org_a.slug,
            "org_b_slug": org_b.slug,
            "sess_a_public_id": sess_a.session_id,
            "sess_a_db_id": sess_a.id,
            "sess_b_public_id": sess_b.session_id,
            "sess_b_db_id": sess_b.id,
            "caller_username": caller.username,
        }
    finally:
        db.close()

    ctx["headers"] = _login(client, ctx["caller_username"], "Password123")
    ctx["headers"]["X-MeetingOps-Org"] = ctx["org_a_slug"]
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_external_only_recipients_send(client, two_orgs_email):
    """No speaker_ids + non-empty additional_recipients should succeed and
    drive one Postmark send per external address.
    """
    sends: list[dict[str, Any]] = []

    def fake_postmark(**kwargs):
        sends.append(kwargs)
        return {"ok": True, "message_id": "stub"}

    with patch("api.session_emails._postmark_send", side_effect=fake_postmark):
        res = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={
                "additional_recipients": ["alice@example.com", "bob@example.com"],
                "include": ["link"],
            },
            headers=two_orgs_email["headers"],
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sent"] == 2
    assert body["skipped"] == 0
    assert body["failures"] == []
    sent_to = sorted(s["to_email"] for s in sends)
    assert sent_to == ["alice@example.com", "bob@example.com"]

    # Each external address minted a SessionCollaborator row scoped to this
    # session only.
    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        rows = (
            db.query(SessionCollaborator)
            .filter(SessionCollaborator.session_id == two_orgs_email["sess_a_db_id"])
            .all()
        )
        emails = sorted(r.email for r in rows)
        assert emails == ["alice@example.com", "bob@example.com"]
        for r in rows:
            assert r.token is not None
            assert r.token_hash is not None
            assert len(r.token_hash) == 64
            assert r.delivery_state == "sent"
            assert r.access_level == "read"
    finally:
        db.close()


def test_combined_speakers_and_externals(client, two_orgs_email):
    """speaker_ids + additional_recipients should fan out to both groups.

    Speakers without a stored email get skipped (no email on file) but the
    external addresses still go out.
    """
    # Seed a speaker without an email so the speaker-fanout has something
    # legitimate to skip on (we don't want a hard 404 here).
    from database.database import SessionLocal
    from database.models import SpeakerProfile

    db = SessionLocal()
    try:
        # Pull the org_a id from the session row to associate the speaker
        # with the same organization that the caller is operating in.
        from database.models import RecordingSession

        sess = db.query(RecordingSession).filter(
            RecordingSession.session_id == two_orgs_email["sess_a_public_id"]
        ).one()
        speaker = SpeakerProfile(
            display_name="No-email Speaker",
            email=None,
            organization_id=sess.organization_id,
        )
        db.add(speaker)
        db.commit()
        db.refresh(speaker)
        speaker_id = speaker.id
    finally:
        db.close()

    sends: list[dict[str, Any]] = []

    def fake_postmark(**kwargs):
        sends.append(kwargs)
        return {"ok": True, "message_id": "stub"}

    with patch("api.session_emails._postmark_send", side_effect=fake_postmark):
        res = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={
                "speaker_ids": [speaker_id],
                "additional_recipients": ["charlie@example.com"],
                "include": ["link"],
            },
            headers=two_orgs_email["headers"],
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sent"] == 1  # only charlie
    assert body["skipped"] == 1  # the no-email speaker
    assert any(f.get("speaker_id") == speaker_id for f in body["failures"])
    assert [s["to_email"] for s in sends] == ["charlie@example.com"]


def test_invalid_email_returns_422(client, two_orgs_email):
    """pydantic EmailStr catches obvious garbage before we hit the handler."""
    res = client.post(
        f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
        json={
            "additional_recipients": ["not-an-email"],
            "include": ["link"],
        },
        headers=two_orgs_email["headers"],
    )
    assert res.status_code == 422, res.text


def test_no_recipients_at_all_returns_400(client, two_orgs_email):
    """Empty speaker_ids + empty additional_recipients on a session with no
    participants should refuse cleanly with a 400."""
    res = client.post(
        f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
        json={
            "additional_recipients": [],
            "include": ["link"],
        },
        headers=two_orgs_email["headers"],
    )
    assert res.status_code == 400, res.text
    assert "no recipients" in res.json()["detail"].lower()


def test_external_token_only_grants_one_session(client, two_orgs_email):
    """The magic-link token an external recipient receives must resolve to
    EXACTLY the one session it was minted for, regardless of org. A token
    minted for session A in org A cannot be used to view session B in
    org B."""
    sends: list[dict[str, Any]] = []

    def fake_postmark(**kwargs):
        sends.append(kwargs)
        return {"ok": True, "message_id": "stub"}

    with patch("api.session_emails._postmark_send", side_effect=fake_postmark):
        res = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={
                "additional_recipients": ["external@example.com"],
                "include": ["link"],
            },
            headers=two_orgs_email["headers"],
        )
    assert res.status_code == 200, res.text

    # Pull the freshly-minted plaintext from the outbound message. The
    # database contains only its digest.
    match = re.search(
        r'href="[^"]*/invite-bootstrap\.html#token=([^"]+)"',
        sends[0]["html_body"],
    )
    assert match
    token = unquote(match.group(1))

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(SessionCollaborator)
            .filter(
                SessionCollaborator.session_id == two_orgs_email["sess_a_db_id"],
                SessionCollaborator.email == "external@example.com",
            )
            .one()
        )
        assert row.token is not None
        compatibility_uuid = str(row.token)
        assert row.token_hash
        leaked_hash = row.token_hash
    finally:
        db.close()

    # The token resolves — but ONLY to session A. We hit the public
    # resolve endpoint (no auth) and check the bound session_id matches A
    # and is NOT session B.
    resolve = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": token},
    )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["valid"] is True
    assert body["session_db_id"] == two_orgs_email["sess_a_db_id"]
    assert body["session_db_id"] != two_orgs_email["sess_b_db_id"]
    # session_id is the public string id and must match A's, not B's
    assert body["session_id"] == two_orgs_email["sess_a_public_id"]
    assert body["session_id"] != two_orgs_email["sess_b_public_id"]

    # A leaked row contains only the digest, which is not a bearer secret.
    leaked = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": leaked_hash},
    )
    assert leaked.status_code == 200
    assert leaked.json()["valid"] is False
    leaked_compatibility = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": compatibility_uuid},
    )
    assert leaked_compatibility.status_code == 200
    assert leaked_compatibility.json()["valid"] is False


def test_email_link_retry_is_rate_limited_after_a_failed_delivery(
    client,
    two_orgs_email,
):
    """The copy workflow cannot duplicate a grant or retry a provider rapidly."""
    attempts: list[dict[str, Any]] = []

    def failed_postmark(**kwargs):
        attempts.append(kwargs)
        return {"ok": False, "error": "provider_private_detail"}

    payload = {
        "additional_recipients": ["retry@example.com"],
        "include": ["link"],
    }
    with patch("api.session_emails._postmark_send", side_effect=failed_postmark):
        first = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json=payload,
            headers=two_orgs_email["headers"],
        )
        repeated = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json=payload,
            headers=two_orgs_email["headers"],
        )

    assert first.status_code == 200, first.text
    assert first.json()["sent"] == 0
    assert first.json()["failures"][0]["reason"] == "delivery failed"
    assert "provider_private_detail" not in first.text
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["sent"] == 0
    assert repeated.json()["skipped"] == 1
    assert repeated.json()["failures"][0]["reason"] == "invitation_attempted_recently"
    assert len(attempts) == 1

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        rows = (
            db.query(SessionCollaborator)
            .filter(
                SessionCollaborator.session_id == two_orgs_email["sess_a_db_id"],
                SessionCollaborator.email == "retry@example.com",
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].delivery_attempt_count == 1
        assert rows[0].delivery_state == "failed"
        assert rows[0].delivery_failure_reason == "delivery_failed"
        assert rows[0].token_hash is not None
    finally:
        db.close()


def test_email_link_issuance_is_controlled_unavailable_while_flag_is_off(
    client,
    two_orgs_email,
    monkeypatch,
):
    monkeypatch.delenv("MEETING_INVITE_V2_ISSUANCE_ENABLED", raising=False)
    email = f"flag-off-{uuid.uuid4().hex[:8]}@example.com"
    with patch("api.session_emails._postmark_send") as sender:
        response = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={"additional_recipients": [email], "include": ["link"]},
            headers=two_orgs_email["headers"],
        )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Invitation link issuance is temporarily unavailable"
    )
    sender.assert_not_called()

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        assert db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == two_orgs_email["sess_a_db_id"],
            SessionCollaborator.email == email,
        ).count() == 0
    finally:
        db.close()


def test_email_link_mint_consolidates_duplicates_at_the_parent_lock_boundary(
    client,
    two_orgs_email,
):
    from api import session_emails
    from services.invitation_tokens import (
        generate_invitation_secret,
        hash_invitation_secret,
    )

    email = f"email-dedupe-{uuid.uuid4().hex[:8]}@example.com"
    old_secrets = [generate_invitation_secret() for _ in range(3)]
    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        rows = []
        for index, secret in enumerate(old_secrets):
            row = SessionCollaborator(
                session_id=two_orgs_email["sess_a_db_id"],
                email=email.upper() if index == 1 else email,
                access_level="read",
                token=uuid.uuid4(),
                token_hash=hash_invitation_secret(secret),
                token_version=2,
                delivery_state="sent",
            )
            db.add(row)
            db.flush()
            rows.append(row)
        db.commit()
        survivor_id = rows[0].id
    finally:
        db.close()

    sends: list[dict[str, Any]] = []

    def fake_postmark(**kwargs):
        sends.append(kwargs)
        return {"ok": True, "message_id": "stub"}

    with (
        patch(
            "api.session_emails._lock_invitation_parent",
            wraps=session_emails._lock_invitation_parent,
        ) as parent_lock,
        patch("api.session_emails._postmark_send", side_effect=fake_postmark),
    ):
        response = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={"additional_recipients": [email], "include": ["link"]},
            headers=two_orgs_email["headers"],
        )
    assert response.status_code == 200, response.text
    assert response.json()["sent"] == 1
    parent_lock.assert_called_once()
    assert len(sends) == 1

    match = re.search(
        r'href="[^"]*/invite-bootstrap\.html#token=([^"]+)"',
        sends[0]["html_body"],
    )
    assert match
    new_secret = unquote(match.group(1))

    db = SessionLocal()
    try:
        rows = db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == two_orgs_email["sess_a_db_id"],
            SessionCollaborator.email.ilike(email),
        ).order_by(SessionCollaborator.id).all()
        assert [row.id for row in rows if row.revoked_at is None] == [survivor_id]
        assert rows[0].delivery_state == "sent"
        assert all(row.delivery_state == "revoked" for row in rows[1:])
        assert rows[0].token_hash == hash_invitation_secret(new_secret)
    finally:
        db.close()

    old_states = [
        client.post(
            "/api/simple/recording-sessions/permissions/access",
            json={"token": secret},
        ).json()
        for secret in old_secrets
    ]
    assert old_states[0]["reason"] == "invitation not found"
    assert [state["reason"] for state in old_states[1:]] == [
        "revoked",
        "revoked",
    ]
    assert client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": new_secret},
    ).json()["valid"] is True


def test_already_authorized_recipient_gets_bearer_free_link_and_email_continues(
    client,
    two_orgs_email,
    monkeypatch,
):
    _, User, _, SessionLocal, _, SessionCollaborator = _models()
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        recipient = User(
            email=f"accepted-{suffix}@example.com",
            username=f"accepted_{suffix}",
            hashed_password=get_password_hash("Password123"),
            is_active=True,
            is_verified=True,
        )
        db.add(recipient)
        db.flush()
        row = SessionCollaborator(
            session_id=two_orgs_email["sess_a_db_id"],
            user_id=recipient.id,
            email=recipient.email,
            access_level="read",
            token=uuid.uuid4(),
            token_hash=None,
            token_version=1,
            accepted_at=None,
            delivery_state="sent",
        )
        db.add(row)
        db.commit()
        recipient_email = recipient.email
        row_id = row.id
    finally:
        db.close()

    # This path needs no new bearer, so it remains usable with issuance off.
    monkeypatch.delenv("MEETING_INVITE_V2_ISSUANCE_ENABLED", raising=False)
    sends: list[dict[str, Any]] = []

    def fake_postmark(**kwargs):
        sends.append(kwargs)
        return {"ok": True, "message_id": "stub"}

    with (
        patch("api.session_emails._build_attachments", return_value=[]),
        patch("api.session_emails._postmark_send", side_effect=fake_postmark),
    ):
        response = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={
                "additional_recipients": [recipient_email],
                "include": ["summary_pdf", "link"],
            },
            headers=two_orgs_email["headers"],
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"sent": 1, "skipped": 0, "failures": []}
    assert len(sends) == 1
    html_body = sends[0]["html_body"]
    assert (
        f"https://meetingops.test/sessions/{two_orgs_email['sess_a_public_id']}"
        in html_body
    )
    assert "invite-bootstrap" not in html_body
    assert "#token=" not in html_body

    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == row_id
        ).one()
        assert row.token_hash is None
        assert row.delivery_state == "accepted"
        assert row.delivery_failure_reason is None
    finally:
        db.close()


def test_email_link_missing_public_url_is_persisted_without_provider_call(
    client,
    two_orgs_email,
    monkeypatch,
):
    monkeypatch.delenv("MEETING_OPS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    email = f"missing-public-{uuid.uuid4().hex[:8]}@example.com"
    with patch("api.session_emails._postmark_send") as sender:
        response = client.post(
            f"/api/simple/recording-sessions/{two_orgs_email['sess_a_public_id']}/email-attendees",
            json={"additional_recipients": [email], "include": ["link"]},
            headers=two_orgs_email["headers"],
        )
    assert response.status_code == 200, response.text
    assert response.json()["sent"] == 0
    assert response.json()["skipped"] == 1
    assert response.json()["failures"][0]["reason"] == (
        "public_url_not_configured"
    )
    sender.assert_not_called()

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == two_orgs_email["sess_a_db_id"],
            SessionCollaborator.email == email,
        ).one()
        assert row.delivery_state == "failed"
        assert row.delivery_failure_reason == "public_url_not_configured"
        assert row.token_hash is not None
    finally:
        db.close()
