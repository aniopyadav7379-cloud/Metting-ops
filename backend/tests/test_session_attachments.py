"""Tests for the session-attachments API.

Covers the contract Aaron asked for:

    - Upload + list + download + delete cycle works.
    - Org-scoped (cross-org returns 404).
    - Cascade delete when the session row is deleted.
    - File size limit enforced (413).
    - Type field is accepted from a known short list AND is permissive
      for forward-compat (unknown types pass through).
    - Storage backend: local fallback used when GARAGE_* env not set
      (the test fixture leaves it unset).
"""
from __future__ import annotations

import io
import os
import uuid
from typing import Any

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession, SessionAttachment

    return Organization, User, UserOrganization, SessionLocal, RecordingSession, SessionAttachment


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
        summary="brief",
        duration=60.0,
        participants=[],
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@pytest.fixture(autouse=True)
def _force_local_storage(monkeypatch, tmp_path):
    """Tests must NOT hit Garage. Even if env vars leak in from the
    host shell, _garage_configured() returns False here."""
    monkeypatch.delenv("GARAGE_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("GARAGE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("GARAGE_SECRET_KEY", raising=False)
    # Redirect local storage into a per-test tmpdir so we don't pollute
    # the repo recordings directory.
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))

    # Force the storage module to re-read env on next import.
    import importlib
    import services.attachment_storage as mod

    importlib.reload(mod)
    # Reset cached S3 client (None means re-init on next call; with
    # env cleared, _garage_configured() returns False and the client
    # stays None).
    mod._s3_client = None
    yield


@pytest.fixture()
def two_orgs_attach(client):
    Organization, _, UserOrganization, SessionLocal, _, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]

    try:
        org_a = Organization(
            name=f"Org Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True
        )
        org_b = Organization(
            name=f"Org Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        caller = _seed_user(
            db, f"caller_{suffix}", "Password123", f"caller_{suffix}@example.com"
        )
        outsider = _seed_user(
            db, f"out_{suffix}", "Password123", f"out_{suffix}@example.com"
        )
        # Caller is admin in org A only. Outsider is a member of org B
        # only — used for the cross-org list test.
        db.add_all([
            UserOrganization(
                user_id=caller.id, organization_id=org_a.id, role="admin"
            ),
            UserOrganization(
                user_id=outsider.id, organization_id=org_b.id, role="user"
            ),
        ])
        db.commit()

        sess_a = _make_session(
            db, organization_id=org_a.id, user_id=caller.id, title="Alpha"
        )
        sess_b = _make_session(
            db, organization_id=org_b.id, user_id=outsider.id, title="Bravo"
        )

        ctx = {
            "org_a_slug": org_a.slug,
            "org_b_slug": org_b.slug,
            "sess_a_pub": sess_a.session_id,
            "sess_a_pk": sess_a.id,
            "sess_b_pub": sess_b.session_id,
            "sess_b_pk": sess_b.id,
            "caller_username": caller.username,
            "outsider_username": outsider.username,
        }
    finally:
        db.close()

    ctx["headers"] = _login(client, ctx["caller_username"], "Password123")
    ctx["headers"]["X-MeetingOps-Org"] = ctx["org_a_slug"]
    ctx["headers_outsider"] = _login(client, ctx["outsider_username"], "Password123")
    ctx["headers_outsider"]["X-MeetingOps-Org"] = ctx["org_b_slug"]
    return ctx


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def _upload_attachment(
    client,
    headers,
    session_pub_id,
    *,
    filename="external-transcript.txt",
    content=b"hi from another vendor",
    attachment_type="transcript",
    source_label=None,
    notes=None,
):
    """Helper that drives the multipart POST end-to-end."""
    data: dict[str, Any] = {}
    if attachment_type is not None:
        data["attachment_type"] = attachment_type
    if source_label is not None:
        data["source_label"] = source_label
    if notes is not None:
        data["notes"] = notes
    return client.post(
        f"/api/simple/recording-sessions/{session_pub_id}/attachments",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
        data=data,
    )


def test_upload_list_download_delete_cycle(client, two_orgs_attach):
    ctx = two_orgs_attach

    # Upload
    payload = b"Granola notes from Mike. Discussed roadmap."
    resp = _upload_attachment(
        client,
        ctx["headers"],
        ctx["sess_a_pub"],
        filename="mikes-notes.md",
        content=payload,
        attachment_type="notes",
        source_label="Granola notes from Mike",
        notes="Mike's perspective on the meeting",
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["filename"] == "mikes-notes.md"
    assert created["size_bytes"] == len(payload)
    assert created["attachment_type"] == "notes"
    assert created["source_label"] == "Granola notes from Mike"
    assert created["storage_backend"] == "local"
    att_id = created["id"]

    # List
    resp = client.get(
        f"/api/simple/recording-sessions/{ctx['sess_a_pub']}/attachments",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == att_id

    # Counts surface
    resp = client.get(
        "/api/simple/recording-sessions-attachment-counts",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    counts = resp.json()
    assert counts.get(str(ctx["sess_a_pk"])) == 1

    # Download
    resp = client.get(
        f"/api/simple/recording-sessions/{ctx['sess_a_pub']}/attachments/{att_id}/download",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == payload
    assert "attachment" in resp.headers.get("content-disposition", "").lower()
    assert "mikes-notes.md" in resp.headers["content-disposition"]

    # Update metadata
    resp = client.put(
        f"/api/simple/recording-sessions/{ctx['sess_a_pub']}/attachments/{att_id}",
        headers=ctx["headers"],
        json={"source_label": "Granola notes from Mike (final)"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source_label"] == "Granola notes from Mike (final)"

    # Delete
    resp = client.delete(
        f"/api/simple/recording-sessions/{ctx['sess_a_pub']}/attachments/{att_id}",
        headers=ctx["headers"],
    )
    assert resp.status_code == 204, resp.text

    # List shows empty
    resp = client.get(
        f"/api/simple/recording-sessions/{ctx['sess_a_pub']}/attachments",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_cross_org_attachment_invisible(client, two_orgs_attach):
    """An org-A user must not see org-B attachments. An attempt to
    list attachments on org-B's session via org-A's header returns 404
    (the session is invisible to them, so `_resolve_session` 404s
    before the attachment query runs)."""
    ctx = two_orgs_attach
    # Upload in org B first
    resp = _upload_attachment(
        client,
        ctx["headers_outsider"],
        ctx["sess_b_pub"],
        filename="bravo-notes.txt",
        content=b"bravo confidential",
    )
    assert resp.status_code == 201, resp.text
    bravo_att_id = resp.json()["id"]

    # Caller in org A tries to list org B's session attachments.
    resp = client.get(
        f"/api/simple/recording-sessions/{ctx['sess_b_pub']}/attachments",
        headers=ctx["headers"],
    )
    assert resp.status_code == 404

    # Caller in org A tries to download org B's attachment.
    resp = client.get(
        f"/api/simple/recording-sessions/{ctx['sess_b_pub']}/attachments/{bravo_att_id}/download",
        headers=ctx["headers"],
    )
    assert resp.status_code == 404

    # And the counts surface only returns counts for org A's sessions.
    resp = client.get(
        "/api/simple/recording-sessions-attachment-counts",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200
    counts = resp.json()
    assert str(ctx["sess_b_pk"]) not in counts


def test_cascade_delete_when_session_deleted(client, two_orgs_attach):
    """Drop the parent session row — the attachment row goes with it
    (FK ON DELETE CASCADE).

    SQLite needs ``PRAGMA foreign_keys = ON`` at connect time to honor
    FK constraints; Postgres honors them by default. We turn the pragma
    on for the duration of this test so the assertion exercises the
    real constraint rather than testing the test fixture's tolerance
    for orphans."""
    ctx = two_orgs_attach
    resp = _upload_attachment(
        client, ctx["headers"], ctx["sess_a_pub"], content=b"to be cascaded"
    )
    assert resp.status_code == 201, resp.text

    _, _, _, SessionLocal, RecordingSession, SessionAttachment = _models()
    db = SessionLocal()
    try:
        # Enable FK enforcement for this session.
        from sqlalchemy import text as _sa_text

        try:
            db.execute(_sa_text("PRAGMA foreign_keys = ON"))
        except Exception:
            # Non-SQLite backend (Postgres): pragma is unrecognized but
            # the constraint is already enforced by the engine.
            pass

        rows_before = db.query(SessionAttachment).filter(
            SessionAttachment.session_id == ctx["sess_a_pk"]
        ).count()
        assert rows_before == 1

        # Hard-delete the session row.
        sess = db.query(RecordingSession).filter(
            RecordingSession.id == ctx["sess_a_pk"]
        ).first()
        db.delete(sess)
        db.commit()

        rows_after = db.query(SessionAttachment).filter(
            SessionAttachment.session_id == ctx["sess_a_pk"]
        ).count()
        assert rows_after == 0, (
            "FK ondelete=CASCADE should have wiped the attachment row "
            "when the session was deleted"
        )
    finally:
        db.close()


def test_oversize_upload_rejected(client, two_orgs_attach, monkeypatch):
    """Pin the 413 response. Smaller-than-prod limit to keep the test
    cheap — patch the constant in-place rather than streaming 100MB."""
    ctx = two_orgs_attach
    from api import session_attachments as mod

    monkeypatch.setattr(mod, "MAX_ATTACHMENT_BYTES", 1024)

    too_big = b"X" * 2048
    resp = _upload_attachment(
        client,
        ctx["headers"],
        ctx["sess_a_pub"],
        filename="oversize.bin",
        content=too_big,
    )
    assert resp.status_code == 413, resp.text


def test_unknown_type_passes_through(client, two_orgs_attach):
    """Forward-compat: the UI may add a new chip ahead of the backend.
    Unknown attachment_type values are accepted and round-trip."""
    ctx = two_orgs_attach
    resp = _upload_attachment(
        client,
        ctx["headers"],
        ctx["sess_a_pub"],
        attachment_type="whiteboard-photo",
        content=b"png-bytes-here",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["attachment_type"] == "whiteboard-photo"


def test_default_type_inferred_when_absent(client, two_orgs_attach):
    """When the caller doesn't send attachment_type the server falls
    back to a mime/filename heuristic."""
    ctx = two_orgs_attach
    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_a_pub']}/attachments",
        headers=ctx["headers"],
        files={"file": ("slides.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
        # No `data=` payload at all — pydantic treats missing Form() as
        # absent rather than None.
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["attachment_type"] == "document"
