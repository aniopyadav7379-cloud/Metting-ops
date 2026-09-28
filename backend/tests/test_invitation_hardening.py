"""Behavior-level coverage for hashed, manager-only meeting invitations."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func

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

    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        SessionCollaborator,
    )


@pytest.fixture()
def invitation_world(client):
    (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        SessionCollaborator,
    ) = _models()
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        host_org = Organization(
            name=f"Invite Host {suffix}",
            slug=f"invite-host-{suffix}",
            is_active=True,
        )
        outside_org = Organization(
            name=f"Invite Outside {suffix}",
            slug=f"invite-outside-{suffix}",
            is_active=True,
        )
        creator = User(
            email=f"invite-creator-{suffix}@example.com",
            username=f"invite_creator_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
            tier="pro",
        )
        viewer = User(
            email=f"invite-viewer-{suffix}@example.com",
            username=f"invite_viewer_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
            tier="pro",
        )
        commenter = User(
            email=f"invite-commenter-{suffix}@example.com",
            username=f"invite_commenter_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
            tier="pro",
        )
        db.add_all([host_org, outside_org, creator, viewer, commenter])
        db.flush()
        db.add_all(
            [
                UserOrganization(
                    user_id=creator.id,
                    organization_id=host_org.id,
                    role="admin",
                ),
                UserOrganization(
                    user_id=viewer.id,
                    organization_id=host_org.id,
                    role="viewer",
                ),
                UserOrganization(
                    user_id=commenter.id,
                    organization_id=outside_org.id,
                    role="viewer",
                ),
            ]
        )
        meeting = RecordingSession(
            session_id=f"invite-session-{suffix}",
            title="Invitation hardening",
            status="completed",
            user_id=creator.id,
            organization_id=host_org.id,
        )
        db.add(meeting)
        db.flush()
        commenter_grant = SessionCollaborator(
            session_id=meeting.id,
            user_id=commenter.id,
            email=commenter.email,
            access_level="comment",
            invited_by_user_id=creator.id,
            token=uuid.uuid4(),
            token_hash=None,
            token_version=2,
            accepted_at=datetime.now(timezone.utc),
            delivery_state="accepted",
        )
        db.add(commenter_grant)
        db.commit()
        context = {
            "host_slug": host_org.slug,
            "outside_slug": outside_org.slug,
            "creator_username": creator.username,
            "viewer_username": viewer.username,
            "commenter_username": commenter.username,
            "commenter_id": commenter.id,
            "commenter_grant_id": commenter_grant.id,
            "meeting_key": meeting.session_id,
            "meeting_id": meeting.id,
            "external_email": f"invite-external-{suffix}@example.com",
        }
    finally:
        db.close()

    def login(username: str, slug: str) -> dict[str, str]:
        response = client.post(
            "/api/auth/login",
            data={"username": username, "password": "admin123"},
        )
        assert response.status_code == 200, response.text
        return {
            "Authorization": f"Bearer {response.json()['access_token']}",
            "X-MeetingOps-Org": slug,
        }

    context["creator_headers"] = login(
        context["creator_username"],
        context["host_slug"],
    )
    context["viewer_headers"] = login(
        context["viewer_username"],
        context["host_slug"],
    )
    context["commenter_headers"] = login(
        context["commenter_username"],
        context["outside_slug"],
    )
    return context


def _secret_from_url(url: str) -> str:
    fragment = parse_qs(urlsplit(url).fragment)
    assert fragment.get("token")
    return fragment["token"][0]


def _all_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def _create_external_invitation(client, world, delivery_result):
    with patch(
        "api.session_permissions._send_invitation_email",
        return_value=delivery_result,
    ) as sender:
        response = client.post(
            (
                f"/api/simple/recording-sessions/{world['meeting_key']}"
                "/permissions/collaborators"
            ),
            headers=world["creator_headers"],
            json={
                "email": world["external_email"],
                "access_level": "read",
            },
        )
    assert response.status_code == 200, response.text
    return response, sender


def test_secret_is_copy_once_hashed_and_a_leaked_row_cannot_redeem(
    client,
    invitation_world,
):
    from api.session_permissions import InvitationDeliveryResult
    from services.invitation_tokens import hash_invitation_secret

    response, sender = _create_external_invitation(
        client,
        invitation_world,
        InvitationDeliveryResult(sent=True),
    )
    body = response.json()
    assert body["created"] is True
    assert body["delivered"] is True
    assert body["invite_url_once"]
    assert "/invite-bootstrap.html#token=" in body["invite_url_once"]
    assert "?token=" not in body["invite_url_once"]
    secret = _secret_from_url(body["invite_url_once"])
    assert sender.call_count == 1

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(SessionCollaborator)
            .filter(
                SessionCollaborator.session_id == invitation_world["meeting_id"],
                SessionCollaborator.email == invitation_world["external_email"],
            )
            .one()
        )
        assert row.token is not None
        compatibility_uuid = str(row.token)
        assert row.token_hash == hash_invitation_secret(secret)
        assert secret not in row.token_hash
        leaked_hash = row.token_hash
    finally:
        db.close()

    valid = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": secret},
    )
    assert valid.status_code == 200
    assert valid.json()["valid"] is True
    assert valid.json()["session_db_id"] == invitation_world["meeting_id"]

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

    listed = client.get(
        (
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
            "/permissions"
        ),
        headers=invitation_world["creator_headers"],
    )
    assert listed.status_code == 200, listed.text
    keys = set(_all_keys(listed.json()))
    assert {"token", "token_hash", "invite_url", "invite_url_once"}.isdisjoint(keys)
    assert secret not in listed.text
    assert leaked_hash not in listed.text

    # Repeating POST updates the existing grant but neither sends nor reveals
    # a fresh secret.
    duplicate, duplicate_sender = _create_external_invitation(
        client,
        invitation_world,
        InvitationDeliveryResult(sent=True),
    )
    assert duplicate.json()["created"] is False
    assert duplicate.json()["invite_url_once"] is None
    assert duplicate_sender.call_count == 0
    db = SessionLocal()
    try:
        assert (
            db.query(SessionCollaborator)
            .filter(
                SessionCollaborator.session_id == invitation_world["meeting_id"],
                SessionCollaborator.email == invitation_world["external_email"],
            )
            .count()
            == 1
        )
    finally:
        db.close()


@pytest.mark.parametrize("actor", ["viewer", "commenter"])
def test_read_and_comment_collaborators_cannot_manage_or_inspect_roster(
    client,
    invitation_world,
    actor,
):
    from api.session_permissions import InvitationDeliveryResult

    created, _ = _create_external_invitation(
        client,
        invitation_world,
        InvitationDeliveryResult(sent=True),
    )
    collaborator_id = created.json()["id"]
    headers = invitation_world[f"{actor}_headers"]
    base = (
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
        "/permissions"
    )

    requests = [
        client.get(base, headers=headers),
        client.post(
            f"{base}/collaborators",
            headers=headers,
            json={"email": "other-private@example.com", "access_level": "read"},
        ),
        client.patch(
            f"{base}/collaborators/{collaborator_id}",
            headers=headers,
            json={"access_level": "edit"},
        ),
        client.post(
            f"{base}/collaborators/{collaborator_id}/resend",
            headers=headers,
        ),
        client.delete(
            f"{base}/collaborators/{collaborator_id}",
            headers=headers,
        ),
    ]
    assert [response.status_code for response in requests] == [403] * 5
    for response in requests:
        assert invitation_world["external_email"] not in response.text
        assert "token_hash" not in response.text
        assert "provider" not in response.text.lower()

    # Both actors may read the meeting itself, but that payload does not carry
    # the private invitation roster.
    meeting = client.get(
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
        headers=headers,
    )
    assert meeting.status_code == 200, meeting.text
    assert invitation_world["external_email"] not in meeting.text


def test_resend_rotates_once_is_rate_limited_and_never_duplicates_grant(
    client,
    invitation_world,
):
    from api.session_permissions import InvitationDeliveryResult

    created, _ = _create_external_invitation(
        client,
        invitation_world,
        InvitationDeliveryResult(
            sent=False,
            failure_reason="delivery_failed",
        ),
    )
    old_secret = _secret_from_url(created.json()["invite_url_once"])
    collaborator_id = created.json()["id"]

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == collaborator_id
        ).one()
        assert row.delivery_state == "failed"
        assert row.delivery_attempt_count == 1
        row.last_delivery_attempt_at = (
            datetime.now(timezone.utc) - timedelta(minutes=2)
        )
        db.commit()
    finally:
        db.close()

    with patch(
        "api.session_permissions._send_invitation_email",
        return_value=InvitationDeliveryResult(
            sent=False,
            failure_reason="delivery_failed",
        ),
    ) as sender:
        resent = client.post(
            (
                f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
                f"/permissions/collaborators/{collaborator_id}/resend"
            ),
            headers=invitation_world["creator_headers"],
        )
        repeated = client.post(
            (
                f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
                f"/permissions/collaborators/{collaborator_id}/resend"
            ),
            headers=invitation_world["creator_headers"],
        )
    assert resent.status_code == 200, resent.text
    assert resent.json()["delivered"] is False
    new_secret = _secret_from_url(resent.json()["invite_url_once"])
    assert new_secret != old_secret
    assert repeated.status_code == 429
    assert int(repeated.headers["Retry-After"]) >= 1
    assert sender.call_count == 1

    old = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": old_secret},
    )
    new = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": new_secret},
    )
    assert old.json()["valid"] is False
    assert new.json()["valid"] is True

    db = SessionLocal()
    try:
        rows = (
            db.query(SessionCollaborator)
            .filter(
                SessionCollaborator.session_id == invitation_world["meeting_id"],
                SessionCollaborator.email == invitation_world["external_email"],
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].delivery_attempt_count == 2
        assert rows[0].delivery_state == "failed"
        assert rows[0].delivery_failure_reason == "delivery_failed"
    finally:
        db.close()


def test_revocation_is_immediate_and_preserves_unrelated_membership(
    client,
    invitation_world,
):
    before = client.get(
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
        headers=invitation_world["commenter_headers"],
    )
    assert before.status_code == 200, before.text

    revoked = client.delete(
        (
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
            "/permissions/collaborators/"
            f"{invitation_world['commenter_grant_id']}"
        ),
        headers=invitation_world["creator_headers"],
    )
    assert revoked.status_code == 200, revoked.text

    after = client.get(
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
        headers=invitation_world["commenter_headers"],
    )
    assert after.status_code == 404

    _, _, UserOrganization, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        grant = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == invitation_world["commenter_grant_id"]
        ).one()
        assert grant.revoked_at is not None
        assert grant.delivery_state == "revoked"
        assert grant.token is not None
        assert (
            db.query(UserOrganization)
            .filter(UserOrganization.user_id == invitation_world["commenter_id"])
            .count()
            == 1
        )
    finally:
        db.close()


def test_migrated_legacy_hash_survives_plaintext_scrub_until_cutoff(
    client,
    invitation_world,
    monkeypatch,
):
    from services.invitation_tokens import hash_invitation_secret

    legacy_secret = str(uuid.uuid4())
    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        row = SessionCollaborator(
            session_id=invitation_world["meeting_id"],
            email=f"legacy-{uuid.uuid4().hex[:6]}@example.com",
            access_level="read",
            token=uuid.UUID(legacy_secret),
            token_hash=hash_invitation_secret(legacy_secret),
            token_version=1,
            delivery_state="sent",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        legacy_row_id = row.id
    finally:
        db.close()

    during_transition = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": legacy_secret},
    )
    assert during_transition.status_code == 200
    assert during_transition.json()["valid"] is True

    # This mirrors the separately approved SQL scrub: only the legacy
    # plaintext column is cleared. The v1 UUID still resolves through its
    # digest while the bounded legacy transport window remains open.
    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == legacy_row_id
        ).one()
        row.token = None
        db.commit()
    finally:
        db.close()
    after_plaintext_scrub = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": legacy_secret},
    )
    assert after_plaintext_scrub.status_code == 200
    assert after_plaintext_scrub.json()["valid"] is True

    monkeypatch.setenv(
        "MEETING_INVITE_LEGACY_TOKEN_CUTOFF",
        "2020-01-01T00:00:00Z",
    )
    after_cutoff = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": legacy_secret},
    )
    assert after_cutoff.status_code == 200
    assert after_cutoff.json()["valid"] is False


def test_redeem_fails_closed_when_an_email_bound_account_has_no_email(
    client,
    invitation_world,
):
    """An empty account email must not bypass an email-bound invitation."""
    from api.session_permissions import InvitationDeliveryResult

    created, _ = _create_external_invitation(
        client,
        invitation_world,
        InvitationDeliveryResult(sent=True),
    )
    secret = _secret_from_url(created.json()["invite_url_once"])
    collaborator_id = created.json()["id"]

    _, User, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            User.id == invitation_world["commenter_id"]
        ).one()
        user.email = ""
        db.commit()
    finally:
        db.close()

    redeemed = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=invitation_world["commenter_headers"],
        json={"token": secret},
    )
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["valid"] is False
    assert redeemed.json()["reason"] == (
        "this invitation requires a verified matching email"
    )

    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == collaborator_id
        ).one()
        assert row.user_id is None
        assert row.accepted_at is None
    finally:
        db.close()


@pytest.mark.parametrize(
    ("terminal_state", "expected_reason"),
    [
        ("invalid", "invitation not found"),
        ("expired", "expired"),
        ("revoked", "revoked"),
    ],
)
def test_terminal_invitation_states_are_controlled_and_secret_free(
    client,
    invitation_world,
    terminal_state,
    expected_reason,
):
    """Bearer holders get a safe terminal state; API errors never echo it."""
    from services.invitation_tokens import (
        generate_invitation_secret,
        hash_invitation_secret,
    )

    secret = generate_invitation_secret()
    collaborator_id = None
    if terminal_state != "invalid":
        _, _, _, SessionLocal, _, SessionCollaborator = _models()
        db = SessionLocal()
        try:
            row = SessionCollaborator(
                session_id=invitation_world["meeting_id"],
                email=f"terminal-{uuid.uuid4().hex[:8]}@example.com",
                access_level="read",
                token=uuid.uuid4(),
                token_hash=hash_invitation_secret(secret),
                token_version=2,
                delivery_state="sent",
                expires_at=(
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                    if terminal_state == "expired"
                    else None
                ),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            collaborator_id = row.id
        finally:
            db.close()

    if terminal_state == "revoked":
        revoked = client.delete(
            (
                f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
                f"/permissions/collaborators/{collaborator_id}"
            ),
            headers=invitation_world["creator_headers"],
        )
        assert revoked.status_code == 200, revoked.text

    response = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": secret},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["reason"] == expected_reason
    assert secret not in response.text
    assert "token_hash" not in response.text


def test_secret_in_a_query_string_is_not_an_invitation_api_contract(
    client,
    invitation_world,
):
    """The public resolver accepts only a JSON body, never a request URL."""
    from services.invitation_tokens import generate_invitation_secret

    secret = generate_invitation_secret()
    response = client.get(
        "/api/simple/recording-sessions/permissions/access",
        params={"token": secret},
    )
    assert response.status_code in {404, 405}
    assert secret not in response.text


def test_native_oidc_return_path_rejects_open_redirect_targets():
    """The bootstrap's fixed native return path cannot be widened by input."""
    from auth.oidc_sso import _safe_return_to

    assert _safe_return_to("/shared/sessions") == "/shared/sessions"
    assert _safe_return_to("https://attacker.example/invite") == "/"
    assert _safe_return_to("//attacker.example/invite") == "/"
    assert _safe_return_to(None) == "/"


def _login_as(client, username: str, *, org_slug: str | None = None):
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": "admin123"},
    )
    assert response.status_code == 200, response.text
    headers = {
        "Authorization": f"Bearer {response.json()['access_token']}",
    }
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def test_email_only_grant_never_authorizes_until_verified_secret_redemption(
    client,
    invitation_world,
):
    from api.session_permissions import InvitationDeliveryResult

    Organization, User, UserOrganization, SessionLocal, _, SessionCollaborator = (
        _models()
    )
    db = SessionLocal()
    try:
        outside_org = db.query(Organization).filter(
            Organization.slug == invitation_world["outside_slug"]
        ).one()
        unverified = User(
            email=invitation_world["external_email"],
            username=f"unverified_{uuid.uuid4().hex[:8]}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=False,
            tier="pro",
        )
        db.add(unverified)
        db.flush()
        db.add(
            UserOrganization(
                user_id=unverified.id,
                organization_id=outside_org.id,
                role="viewer",
            )
        )
        db.commit()
        username = unverified.username
        user_id = unverified.id
    finally:
        db.close()

    headers = _login_as(
        client,
        username,
        org_slug=invitation_world["outside_slug"],
    )
    created, _ = _create_external_invitation(
        client,
        invitation_world,
        InvitationDeliveryResult(sent=True),
    )
    secret = _secret_from_url(created.json()["invite_url_once"])

    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == created.json()["id"]
        ).one()
        assert row.user_id is None
    finally:
        db.close()

    before = client.get(
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
        headers=headers,
    )
    assert before.status_code == 404

    wrong = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=headers,
        json={"token": "wrong-secret-value-that-is-long-enough"},
    )
    assert wrong.json() == {
        "valid": False,
        "session_id": None,
        "session_db_id": None,
        "access_level": None,
        "reason": "invitation not found",
        "bound_user_id": None,
    }
    unverified_redeem = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=headers,
        json={"token": secret},
    )
    assert unverified_redeem.status_code == 200
    assert unverified_redeem.json()["valid"] is False
    assert unverified_redeem.json()["reason"] == (
        "this invitation requires a verified matching email"
    )

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
        user.is_verified = True
        db.commit()
    finally:
        db.close()

    still_not_bound = client.get(
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
        headers=headers,
    )
    assert still_not_bound.status_code == 404
    correct = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=headers,
        json={"token": secret},
    )
    assert correct.status_code == 200
    assert correct.json()["valid"] is True
    assert correct.json()["bound_user_id"] == user_id

    after = client.get(
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
        headers=headers,
    )
    assert after.status_code == 200, after.text


def test_verified_email_auto_resolution_and_explicit_user_id_grants_are_bound(
    client,
    invitation_world,
    monkeypatch,
):
    Organization, User, UserOrganization, SessionLocal, _, SessionCollaborator = (
        _models()
    )
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        outside_org = db.query(Organization).filter(
            Organization.slug == invitation_world["outside_slug"]
        ).one()
        verified = User(
            email=f"verified-direct-{suffix}@example.com",
            username=f"verified_direct_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
            tier="pro",
        )
        explicit = User(
            email=f"explicit-direct-{suffix}@example.com",
            username=f"explicit_direct_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=False,
            tier="pro",
        )
        db.add_all([verified, explicit])
        db.flush()
        db.add_all(
            [
                UserOrganization(
                    user_id=verified.id,
                    organization_id=outside_org.id,
                    role="viewer",
                ),
                UserOrganization(
                    user_id=explicit.id,
                    organization_id=outside_org.id,
                    role="viewer",
                ),
            ]
        )
        db.commit()
        verified_id = verified.id
        explicit_id = explicit.id
        verified_email = verified.email
        verified_username = verified.username
        explicit_username = explicit.username
    finally:
        db.close()

    # Direct user grants do not mint bearer secrets, so they remain available
    # while mixed-version v2 issuance is deliberately off.
    monkeypatch.delenv("MEETING_INVITE_V2_ISSUANCE_ENABLED", raising=False)
    base = (
        f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
        "/permissions/collaborators"
    )
    by_verified_email = client.post(
        base,
        headers=invitation_world["creator_headers"],
        json={"email": verified_email, "access_level": "read"},
    )
    assert by_verified_email.status_code == 200, by_verified_email.text
    assert by_verified_email.json()["user"]["id"] == verified_id
    assert by_verified_email.json()["invite_url_once"] is None

    by_explicit_id = client.post(
        base,
        headers=invitation_world["creator_headers"],
        json={"user_id": explicit_id, "access_level": "comment"},
    )
    assert by_explicit_id.status_code == 200, by_explicit_id.text
    assert by_explicit_id.json()["user"]["id"] == explicit_id
    assert by_explicit_id.json()["invite_url_once"] is None

    for username in (verified_username, explicit_username):
        headers = _login_as(
            client,
            username,
            org_slug=invitation_world["outside_slug"],
        )
        response = client.get(
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}",
            headers=headers,
        )
        assert response.status_code == 200, response.text

    db = SessionLocal()
    try:
        rows = db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == invitation_world["meeting_id"],
            SessionCollaborator.user_id.in_([verified_id, explicit_id]),
            SessionCollaborator.revoked_at.is_(None),
        ).all()
        assert len(rows) == 2
        assert all(row.token_hash is None for row in rows)
        assert all(row.delivery_state == "accepted" for row in rows)
    finally:
        db.close()


def test_v2_issuance_defaults_off_while_v1_redemption_remains_available(
    client,
    invitation_world,
    monkeypatch,
):
    from api.session_permissions import InvitationDeliveryResult
    from services.invitation_tokens import (
        generate_invitation_secret,
        hash_invitation_secret,
    )

    monkeypatch.delenv("MEETING_INVITE_V2_ISSUANCE_ENABLED", raising=False)
    unique_email = f"flag-off-{uuid.uuid4().hex[:8]}@example.com"
    with patch(
        "api.session_permissions._send_invitation_email",
        return_value=InvitationDeliveryResult(sent=True),
    ) as sender:
        unavailable = client.post(
            (
                f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
                "/permissions/collaborators"
            ),
            headers=invitation_world["creator_headers"],
            json={"email": unique_email, "access_level": "read"},
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == (
        "Invitation link issuance is temporarily unavailable"
    )
    sender.assert_not_called()

    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    legacy_secret = str(uuid.uuid4())
    pending_secret = generate_invitation_secret()
    db = SessionLocal()
    try:
        legacy = SessionCollaborator(
            session_id=invitation_world["meeting_id"],
            email=f"legacy-flag-{uuid.uuid4().hex[:8]}@example.com",
            access_level="read",
            token=uuid.UUID(legacy_secret),
            token_hash=hash_invitation_secret(legacy_secret),
            token_version=1,
            delivery_state="sent",
        )
        pending = SessionCollaborator(
            session_id=invitation_world["meeting_id"],
            email=f"pending-flag-{uuid.uuid4().hex[:8]}@example.com",
            access_level="read",
            token=uuid.uuid4(),
            token_hash=hash_invitation_secret(pending_secret),
            token_version=2,
            delivery_state="failed",
            last_delivery_attempt_at=datetime.now(timezone.utc) - timedelta(
                minutes=5
            ),
        )
        db.add_all([legacy, pending])
        db.commit()
        pending_id = pending.id
        pending_hash = pending.token_hash
        assert db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == invitation_world["meeting_id"],
            SessionCollaborator.email == unique_email,
        ).count() == 0
    finally:
        db.close()

    legacy_access = client.post(
        "/api/simple/recording-sessions/permissions/access",
        json={"token": legacy_secret},
    )
    assert legacy_access.status_code == 200
    assert legacy_access.json()["valid"] is True

    resend = client.post(
        (
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
            f"/permissions/collaborators/{pending_id}/resend"
        ),
        headers=invitation_world["creator_headers"],
    )
    assert resend.status_code == 503
    db = SessionLocal()
    try:
        pending = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == pending_id
        ).one()
        assert pending.token_hash == pending_hash
        assert pending.delivery_attempt_count == 0
    finally:
        db.close()


def test_add_consolidates_duplicate_identity_deterministically_under_parent_lock(
    client,
    invitation_world,
):
    from api import session_permissions
    from services.invitation_tokens import (
        generate_invitation_secret,
        hash_invitation_secret,
    )

    email = f"dedupe-{uuid.uuid4().hex[:8]}@example.com"
    secrets = [generate_invitation_secret() for _ in range(3)]
    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        rows = []
        for index, secret in enumerate(secrets):
            row = SessionCollaborator(
                session_id=invitation_world["meeting_id"],
                email=email.upper() if index == 1 else email,
                access_level="read",
                token=uuid.uuid4(),
                token_hash=hash_invitation_secret(secret),
                token_version=2,
                delivery_state="sent",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
            )
            db.add(row)
            db.flush()
            rows.append(row)
        db.commit()
        survivor_id = rows[0].id
    finally:
        db.close()

    with patch(
        "api.session_permissions._lock_invitation_parent",
        wraps=session_permissions._lock_invitation_parent,
    ) as parent_lock:
        response = client.post(
            (
                f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
                "/permissions/collaborators"
            ),
            headers=invitation_world["creator_headers"],
            json={"email": email, "access_level": "comment"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["created"] is False
    assert response.json()["id"] == survivor_id
    parent_lock.assert_called_once()

    db = SessionLocal()
    try:
        rows = db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == invitation_world["meeting_id"],
            func.lower(SessionCollaborator.email) == email,
        ).order_by(SessionCollaborator.id).all()
        assert [row.id for row in rows if row.revoked_at is None] == [survivor_id]
        assert rows[0].access_level == "comment"
        assert all(row.delivery_state == "revoked" for row in rows[1:])
    finally:
        db.close()

    states = [
        client.post(
            "/api/simple/recording-sessions/permissions/access",
            json={"token": secret},
        ).json()
        for secret in secrets
    ]
    assert states[0]["valid"] is True
    assert [state["reason"] for state in states[1:]] == ["revoked", "revoked"]


def test_revocation_revokes_every_active_sibling_secret(
    client,
    invitation_world,
):
    from services.invitation_tokens import (
        generate_invitation_secret,
        hash_invitation_secret,
    )

    email = f"revoke-all-{uuid.uuid4().hex[:8]}@example.com"
    secrets = [generate_invitation_secret() for _ in range(3)]
    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        rows = []
        for secret in secrets:
            row = SessionCollaborator(
                session_id=invitation_world["meeting_id"],
                email=email,
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
        target_id = rows[1].id
    finally:
        db.close()

    response = client.delete(
        (
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
            f"/permissions/collaborators/{target_id}"
        ),
        headers=invitation_world["creator_headers"],
    )
    assert response.status_code == 200, response.text

    db = SessionLocal()
    try:
        rows = db.query(SessionCollaborator).filter(
            SessionCollaborator.session_id == invitation_world["meeting_id"],
            SessionCollaborator.email == email,
        ).all()
        assert len(rows) == 3
        assert all(row.revoked_at is not None for row in rows)
        assert all(row.delivery_state == "revoked" for row in rows)
    finally:
        db.close()

    for secret in secrets:
        resolved = client.post(
            "/api/simple/recording-sessions/permissions/access",
            json={"token": secret},
        )
        assert resolved.json()["valid"] is False
        assert resolved.json()["reason"] == "revoked"


def test_redeem_is_first_writer_and_idempotent_for_the_bound_user(
    client,
    invitation_world,
):
    from auth.models import User
    from services.invitation_tokens import (
        generate_invitation_secret,
        hash_invitation_secret,
    )

    suffix = uuid.uuid4().hex[:8]
    _, _, _, SessionLocal, _, SessionCollaborator = _models()
    db = SessionLocal()
    try:
        first_user = User(
            email=f"first-writer-{suffix}@example.com",
            username=f"first_writer_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
        )
        second_user = User(
            email=f"second-writer-{suffix}@example.com",
            username=f"second_writer_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
        )
        db.add_all([first_user, second_user])
        db.flush()
        secret = generate_invitation_secret()
        row = SessionCollaborator(
            session_id=invitation_world["meeting_id"],
            email=None,
            access_level="read",
            token=uuid.uuid4(),
            token_hash=hash_invitation_secret(secret),
            token_version=2,
            delivery_state="sent",
        )
        db.add(row)
        db.commit()
        row_id = row.id
        first_id = first_user.id
        first_username = first_user.username
        second_username = second_user.username
    finally:
        db.close()

    first_headers = _login_as(client, first_username)
    second_headers = _login_as(client, second_username)
    first = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=first_headers,
        json={"token": secret},
    )
    assert first.status_code == 200
    assert first.json()["valid"] is True
    assert first.json()["bound_user_id"] == first_id

    second = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=second_headers,
        json={"token": secret},
    )
    assert second.status_code == 200
    assert second.json()["valid"] is False
    assert second.json()["reason"] == (
        "this invitation belongs to a different account"
    )

    repeated = client.post(
        "/api/simple/recording-sessions/permissions/redeem",
        headers=first_headers,
        json={"token": secret},
    )
    assert repeated.status_code == 200
    assert repeated.json()["valid"] is True
    assert repeated.json()["bound_user_id"] == first_id

    db = SessionLocal()
    try:
        row = db.query(SessionCollaborator).filter(
            SessionCollaborator.id == row_id
        ).one()
        assert row.user_id == first_id
        assert row.accepted_at is not None
        assert row.delivery_state == "accepted"
    finally:
        db.close()


def test_legacy_user_bound_grant_is_accepted_and_cannot_be_resent(
    client,
    invitation_world,
):
    Organization, User, _, SessionLocal, _, SessionCollaborator = _models()
    suffix = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        user = User(
            email=f"legacy-bound-{suffix}@example.com",
            username=f"legacy_bound_{suffix}",
            hashed_password=get_password_hash("admin123"),
            is_active=True,
            is_verified=True,
        )
        db.add(user)
        db.flush()
        row = SessionCollaborator(
            session_id=invitation_world["meeting_id"],
            user_id=user.id,
            email=user.email,
            access_level="read",
            token=uuid.uuid4(),
            token_hash=None,
            token_version=1,
            accepted_at=None,
            delivery_state="sent",
            last_delivery_attempt_at=datetime.now(timezone.utc) - timedelta(
                minutes=5
            ),
        )
        db.add(row)
        db.commit()
        row_id = row.id
    finally:
        db.close()

    listed = client.get(
        (
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
            "/permissions"
        ),
        headers=invitation_world["creator_headers"],
    )
    assert listed.status_code == 200
    listed_row = next(item for item in listed.json()["collaborators"] if item["id"] == row_id)
    assert listed_row["delivery_state"] == "accepted"

    resent = client.post(
        (
            f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
            f"/permissions/collaborators/{row_id}/resend"
        ),
        headers=invitation_world["creator_headers"],
    )
    assert resent.status_code == 409
    assert "accepted" in resent.json()["detail"]


def test_public_url_policy_fails_closed_but_manual_copy_can_be_relative(
    client,
    invitation_world,
    monkeypatch,
):
    from api.session_permissions import InvitationDeliveryResult
    from services.invitation_tokens import build_invitation_url

    monkeypatch.delenv("MEETING_OPS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    with pytest.raises(ValueError, match="public_url_not_configured"):
        build_invitation_url("secret-value", require_public=True)
    assert build_invitation_url("secret-value").startswith(
        "/invite-bootstrap.html#token="
    )

    monkeypatch.setenv("MEETING_OPS_PUBLIC_URL", "http://public.example")
    with pytest.raises(ValueError, match="public_url_not_configured"):
        build_invitation_url("secret-value", require_public=True)
    monkeypatch.setenv("MEETING_OPS_PUBLIC_URL", "http://127.0.0.1:7777")
    assert build_invitation_url(
        "secret-value",
        require_public=True,
    ).startswith("http://127.0.0.1:7777/")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="public_url_not_configured"):
        build_invitation_url("secret-value", require_public=True)

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("MEETING_OPS_PUBLIC_URL", raising=False)
    with patch(
        "api.session_permissions._send_invitation_email",
        return_value=InvitationDeliveryResult(sent=True),
    ) as sender:
        created = client.post(
            (
                f"/api/simple/recording-sessions/{invitation_world['meeting_key']}"
                "/permissions/collaborators"
            ),
            headers=invitation_world["creator_headers"],
            json={
                "email": f"no-public-url-{uuid.uuid4().hex[:8]}@example.com",
                "access_level": "read",
            },
        )
    assert created.status_code == 200, created.text
    assert created.json()["delivered"] is False
    assert created.json()["invite_url_once"].startswith(
        "/invite-bootstrap.html#token="
    )
    assert created.json()["delivery_failure_reason"] == (
        "public_url_not_configured"
    )
    sender.assert_not_called()


def test_smtp_plaintext_is_loopback_dev_only_and_remote_uses_validated_tls(
    invitation_world,
    monkeypatch,
):
    from api.session_permissions import _send_invitation_email

    _, User, _, SessionLocal, RecordingSession, _ = _models()
    db = SessionLocal()
    try:
        inviter = db.query(User).filter(
            User.username == invitation_world["creator_username"]
        ).one()
        session = db.query(RecordingSession).filter(
            RecordingSession.id == invitation_world["meeting_id"]
        ).one()

        monkeypatch.setenv("SMTP_FROM", "sender@example.com")
        monkeypatch.setenv("SMTP_HOST", "smtp.remote.example")
        monkeypatch.setenv("SMTP_USE_TLS", "false")
        with (
            patch("auth.email._postmark_token", return_value=""),
            patch("auth.email._postmark_sender", return_value=""),
            patch("smtplib.SMTP") as smtp,
        ):
            blocked = _send_invitation_email(
                to_email="recipient@example.com",
                invite_url="https://meetingops.test/invite-bootstrap.html#token=x",
                inviter=inviter,
                session=session,
                access_level="read",
            )
        assert blocked.sent is False
        assert blocked.failure_reason == "insecure_smtp_not_allowed"
        smtp.assert_not_called()

        monkeypatch.setenv("SMTP_USE_TLS", "true")
        smtp_context = MagicMock()
        smtp_client = MagicMock()
        smtp_context.return_value.__enter__.return_value = smtp_client
        with (
            patch("auth.email._postmark_token", return_value=""),
            patch("auth.email._postmark_sender", return_value=""),
            patch("smtplib.SMTP", smtp_context),
        ):
            delivered = _send_invitation_email(
                to_email="recipient@example.com",
                invite_url="https://meetingops.test/invite-bootstrap.html#token=x",
                inviter=inviter,
                session=session,
                access_level="read",
            )
        assert delivered.sent is True
        smtp_client.starttls.assert_called_once()
        assert smtp_client.starttls.call_args.kwargs["context"] is not None
        smtp_client.send_message.assert_called_once()

        monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("SMTP_USE_TLS", "false")
        loopback_context = MagicMock()
        loopback_client = MagicMock()
        loopback_context.return_value.__enter__.return_value = loopback_client
        with (
            patch("auth.email._postmark_token", return_value=""),
            patch("auth.email._postmark_sender", return_value=""),
            patch("smtplib.SMTP", loopback_context),
        ):
            loopback = _send_invitation_email(
                to_email="recipient@example.com",
                invite_url="https://meetingops.test/invite-bootstrap.html#token=x",
                inviter=inviter,
                session=session,
                access_level="read",
            )
        assert loopback.sent is True
        loopback_client.starttls.assert_not_called()
        loopback_client.send_message.assert_called_once()
    finally:
        db.close()
