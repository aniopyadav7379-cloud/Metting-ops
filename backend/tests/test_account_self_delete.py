"""Tests for self-serve account deletion (DELETE /api/auth/me).

Covers the FK-integrity blockers that made every delete 500 (UserOrganization
memberships + invited_by backrefs, UploadJob/TtsJob user FKs), full data
removal (sessions/transcriptions/jobs), tenancy isolation, the post-commit
best-effort external purge (Qdrant + media — must never fail the request),
and credential death after deletion.

Runs offline on the SQLite fixture (tests/conftest.py). UploadJob/TtsJob rows
are inserted directly via SessionLocal — there is no API helper for them.
"""
from __future__ import annotations

import uuid

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, PersonalAccessToken, User, UserOrganization
    from database.database import SessionLocal
    from database.models import ChatHistory, RecordingSession, Transcription, TtsJob, UploadJob

    return {
        "Organization": Organization,
        "PersonalAccessToken": PersonalAccessToken,
        "User": User,
        "UserOrganization": UserOrganization,
        "SessionLocal": SessionLocal,
        "ChatHistory": ChatHistory,
        "RecordingSession": RecordingSession,
        "Transcription": Transcription,
        "TtsJob": TtsJob,
        "UploadJob": UploadJob,
    }


def _seed_user_org(*, slug: str, username: str):
    m = _models()
    db = m["SessionLocal"]()
    try:
        org = db.query(m["Organization"]).filter(m["Organization"].slug == slug).first()
        if not org:
            org = m["Organization"](
                name=slug.replace("-", " ").title(), slug=slug, is_active=True
            )
            db.add(org)
            db.commit()
            db.refresh(org)

        user = db.query(m["User"]).filter(m["User"].username == username).first()
        if not user:
            user = m["User"](
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
            db.query(m["UserOrganization"])
            .filter(
                m["UserOrganization"].user_id == user.id,
                m["UserOrganization"].organization_id == org.id,
            )
            .first()
        )
        if not membership:
            db.add(
                m["UserOrganization"](user_id=user.id, organization_id=org.id, role="admin")
            )
            db.commit()

        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _seed_session(*, org_id: int, user_id: int, title: str = "Self-Delete Test Session"):
    m = _models()
    db = m["SessionLocal"]()
    try:
        session = m["RecordingSession"](
            session_id=str(uuid.uuid4()),
            title=title,
            name=title,
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            transcript_simple="Discussed deleting accounts properly.",
            tags=[],
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session
    finally:
        db.close()


def _seed_transcription(*, session_pk: int, text: str = "hello world"):
    m = _models()
    db = m["SessionLocal"]()
    try:
        row = m["Transcription"](session_id=session_pk, text=text, speaker="S1")
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _seed_chat_history(*, org_id: int, session_key: str, content: str):
    m = _models()
    db = m["SessionLocal"]()
    try:
        row = m["ChatHistory"](
            session_key=session_key, role="user", content=content,
            organization_id=org_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _seed_upload_job(*, org_id: int, user_id: int, session_pk: int | None = None):
    m = _models()
    db = m["SessionLocal"]()
    try:
        job = m["UploadJob"](
            organization_id=org_id,
            user_id=user_id,
            filename="meeting.wav",
            total_size=1234,
            session_id=session_pk,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def _seed_tts_job(*, org_id: int, user_id: int, session_pk: int):
    m = _models()
    db = m["SessionLocal"]()
    try:
        job = m["TtsJob"](
            organization_id=org_id,
            user_id=user_id,
            session_id=session_pk,
            kind="summary",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def _login_headers(client, username: str, org_slug: str | None = None):
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": "admin123"}
    )
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


# ---------------------------------------------------------------------------
# (i) unauthenticated
# ---------------------------------------------------------------------------

def test_unauthenticated_delete_me_returns_401(client):
    resp = client.delete("/api/auth/me")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# (ii) plain user: row + memberships + PATs gone
# ---------------------------------------------------------------------------

def test_plain_user_delete_removes_user_memberships_and_pats(client):
    m = _models()
    org, user = _seed_user_org(slug="selfdel-plain", username="selfdel_plain_user")
    uid = user.id
    headers = _login_headers(client, "selfdel_plain_user", org.slug)

    # Give the user a PAT so the cascade is actually exercised.
    pat_resp = client.post("/api/auth/pats", headers=headers, json={"name": "Doomed PAT"})
    assert pat_resp.status_code == 201, pat_resp.text

    resp = client.delete("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Account deleted"

    db = m["SessionLocal"]()
    try:
        assert db.query(m["User"]).filter(m["User"].id == uid).first() is None
        assert (
            db.query(m["UserOrganization"])
            .filter(m["UserOrganization"].user_id == uid)
            .count()
            == 0
        )
        assert (
            db.query(m["PersonalAccessToken"])
            .filter(m["PersonalAccessToken"].user_id == uid)
            .count()
            == 0
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# (iii) data removal: sessions / transcriptions / upload+tts jobs, plus the
#       post-commit external purge fires per session and NEVER fails the
#       request (a raising Qdrant purge still returns 200 and still lets the
#       media purge run).
# ---------------------------------------------------------------------------

def test_delete_with_data_removes_rows_and_purges_external(client, monkeypatch):
    m = _models()
    org, user = _seed_user_org(slug="selfdel-data", username="selfdel_data_user")
    uid = user.id
    s1 = _seed_session(org_id=org.id, user_id=uid, title="Session One")
    s2 = _seed_session(org_id=org.id, user_id=uid, title="Session Two")
    _seed_transcription(session_pk=s1.id)
    _seed_transcription(session_pk=s2.id)
    # One in-flight (session-less) upload job + one bound to a session.
    _seed_upload_job(org_id=org.id, user_id=uid, session_pk=None)
    _seed_upload_job(org_id=org.id, user_id=uid, session_pk=s1.id)
    _seed_tts_job(org_id=org.id, user_id=uid, session_pk=s1.id)

    qdrant_calls: list[str] = []
    media_calls: list[str] = []

    def _fake_qdrant_delete(self, session_id):
        qdrant_calls.append(session_id)
        if session_id == s2.session_id:
            raise RuntimeError("qdrant down")  # must be swallowed

    import services.media_storage as media_storage
    from services.semantic_search_service import SemanticSearchService

    monkeypatch.setattr(SemanticSearchService, "delete_session", _fake_qdrant_delete)
    monkeypatch.setattr(
        media_storage,
        "delete_prefix",
        lambda *, prefix: media_calls.append(prefix) or 0,
    )

    headers = _login_headers(client, "selfdel_data_user", org.slug)
    resp = client.delete("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text

    db = m["SessionLocal"]()
    try:
        assert (
            db.query(m["RecordingSession"])
            .filter(m["RecordingSession"].user_id == uid)
            .count()
            == 0
        )
        assert (
            db.query(m["Transcription"])
            .filter(m["Transcription"].session_id.in_([s1.id, s2.id]))
            .count()
            == 0
        )
        assert (
            db.query(m["UploadJob"]).filter(m["UploadJob"].user_id == uid).count() == 0
        )
        assert db.query(m["TtsJob"]).filter(m["TtsJob"].user_id == uid).count() == 0
        assert db.query(m["User"]).filter(m["User"].id == uid).first() is None
    finally:
        db.close()

    # External purge attempted for BOTH sessions, keyed by the canonical
    # string session_id; the s2 Qdrant failure didn't abort anything.
    assert sorted(qdrant_calls) == sorted([s1.session_id, s2.session_id])
    assert sorted(media_calls) == sorted(
        [f"{org.id}/{s1.session_id}/", f"{org.id}/{s2.session_id}/"]
    )


# ---------------------------------------------------------------------------
# (iv) tenancy: user B's account + data survive user A's self-delete
# ---------------------------------------------------------------------------

def test_delete_does_not_touch_other_users_data(client, monkeypatch):
    m = _models()
    org_a, user_a = _seed_user_org(slug="selfdel-org-a", username="selfdel_user_a")
    org_b, user_b = _seed_user_org(slug="selfdel-org-b", username="selfdel_user_b")
    uid_a, uid_b = user_a.id, user_b.id

    # B was invited into org A by A — the invited_by FK must be nulled, NOT
    # the membership deleted, when A deletes their account.
    db = m["SessionLocal"]()
    try:
        db.add(
            m["UserOrganization"](
                user_id=uid_b, organization_id=org_a.id, role="user", invited_by=uid_a
            )
        )
        db.commit()
    finally:
        db.close()

    session_a = _seed_session(org_id=org_a.id, user_id=uid_a, title="A Private")
    session_b = _seed_session(org_id=org_b.id, user_id=uid_b, title="B Private")
    _seed_transcription(session_pk=session_b.id, text="b keeps this")
    b_tts = _seed_tts_job(org_id=org_b.id, user_id=uid_b, session_pk=session_b.id)
    b_upload = _seed_upload_job(org_id=org_b.id, user_id=uid_b, session_pk=None)
    # A queued a TTS job on B's shared session — must die with A (it FKs
    # A's user row), while B's session itself survives.
    _seed_tts_job(org_id=org_b.id, user_id=uid_a, session_pk=session_b.id)

    # Quiet, deterministic external purge (A owns a session, so it fires).
    from services.semantic_search_service import SemanticSearchService

    monkeypatch.setattr(
        SemanticSearchService, "delete_session", lambda self, session_id: None
    )

    headers_a = _login_headers(client, "selfdel_user_a", org_a.slug)
    resp = client.delete("/api/auth/me", headers=headers_a)
    assert resp.status_code == 200, resp.text

    db = m["SessionLocal"]()
    try:
        # A is fully gone.
        assert db.query(m["User"]).filter(m["User"].id == uid_a).first() is None
        assert (
            db.query(m["RecordingSession"])
            .filter(m["RecordingSession"].id == session_a.id)
            .first()
            is None
        )
        assert db.query(m["TtsJob"]).filter(m["TtsJob"].user_id == uid_a).count() == 0

        # B is untouched: account, sessions, transcript, jobs.
        assert db.query(m["User"]).filter(m["User"].id == uid_b).first() is not None
        assert (
            db.query(m["RecordingSession"])
            .filter(m["RecordingSession"].id == session_b.id)
            .first()
            is not None
        )
        assert (
            db.query(m["Transcription"])
            .filter(m["Transcription"].session_id == session_b.id)
            .count()
            == 1
        )
        assert db.query(m["TtsJob"]).filter(m["TtsJob"].id == b_tts).first() is not None
        assert (
            db.query(m["UploadJob"]).filter(m["UploadJob"].id == b_upload).first()
            is not None
        )

        # B keeps BOTH memberships; the org-A one just loses its inviter.
        b_in_a = (
            db.query(m["UserOrganization"])
            .filter(
                m["UserOrganization"].user_id == uid_b,
                m["UserOrganization"].organization_id == org_a.id,
            )
            .first()
        )
        assert b_in_a is not None
        assert b_in_a.invited_by is None
        assert (
            db.query(m["UserOrganization"])
            .filter(
                m["UserOrganization"].user_id == uid_b,
                m["UserOrganization"].organization_id == org_b.id,
            )
            .first()
            is not None
        )
    finally:
        db.close()

    # B can still authenticate.
    assert _login_headers(client, "selfdel_user_b", org_b.slug)


# ---------------------------------------------------------------------------
# (iv-b) cross-tenant chat-history isolation on account delete: a colliding
#        session_key in ANOTHER org must survive (session_key is NOT globally
#        unique — legacy sessions use the integer pk string).
# ---------------------------------------------------------------------------

def test_account_delete_chat_history_is_org_scoped(client, monkeypatch):
    m = _models()
    org_a, user_a = _seed_user_org(slug="chatdel-a", username="chatdel_user_a")
    org_b, user_b = _seed_user_org(slug="chatdel-b", username="chatdel_user_b")
    uid_a = user_a.id

    # A owns a session; its canonical key is the session_id. Seed A's chat
    # history under that key, AND seed B's chat history under the SAME key (the
    # collision the old session_key-only delete would have wiped cross-tenant).
    session_a = _seed_session(org_id=org_a.id, user_id=uid_a, title="A chat session")
    colliding_key = session_a.session_id
    a_chat = _seed_chat_history(org_id=org_a.id, session_key=colliding_key, content="A secret")
    b_chat = _seed_chat_history(org_id=org_b.id, session_key=colliding_key, content="B keeps this")

    from services.semantic_search_service import SemanticSearchService
    monkeypatch.setattr(SemanticSearchService, "delete_session", lambda self, session_id: None)

    headers_a = _login_headers(client, "chatdel_user_a", org_a.slug)
    resp = client.delete("/api/auth/me", headers=headers_a)
    assert resp.status_code == 200, resp.text

    db = m["SessionLocal"]()
    try:
        # A's chat (org_a, key) is gone; B's chat (org_b, SAME key) SURVIVES.
        assert db.query(m["ChatHistory"]).filter(m["ChatHistory"].id == a_chat).first() is None
        surviving = db.query(m["ChatHistory"]).filter(m["ChatHistory"].id == b_chat).first()
        assert surviving is not None, "cross-tenant: B's chat history was wrongly deleted"
        assert surviving.organization_id == org_b.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# (v) admin DELETE /users/{id} shares the FK fix: previously 500'd with an
#     IntegrityError for ANY user with an org membership (all of them).
# ---------------------------------------------------------------------------

def test_admin_delete_user_succeeds_and_removes_memberships(client):
    m = _models()
    org, target = _seed_user_org(slug="selfdel-admin", username="selfdel_admin_target")
    target_id = target.id
    _seed_upload_job(org_id=org.id, user_id=target_id, session_pk=None)

    admin_headers = _login_headers(client, "admin")  # conftest-seeded superuser
    resp = client.delete(f"/api/auth/users/{target_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text

    db = m["SessionLocal"]()
    try:
        assert db.query(m["User"]).filter(m["User"].id == target_id).first() is None
        assert (
            db.query(m["UserOrganization"])
            .filter(m["UserOrganization"].user_id == target_id)
            .count()
            == 0
        )
        assert (
            db.query(m["UploadJob"]).filter(m["UploadJob"].user_id == target_id).count()
            == 0
        )
        # The admin who performed the delete is untouched.
        assert (
            db.query(m["User"]).filter(m["User"].username == "admin").first()
            is not None
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# (vi) double delete: the credential dies with the account
# ---------------------------------------------------------------------------

def test_double_delete_second_call_is_401(client):
    org, _user = _seed_user_org(slug="selfdel-double", username="selfdel_double_user")
    headers = _login_headers(client, "selfdel_double_user", org.slug)

    first = client.delete("/api/auth/me", headers=headers)
    assert first.status_code == 200, first.text

    second = client.delete("/api/auth/me", headers=headers)
    assert second.status_code == 401
