"""Batch-export email delivery (api/batch_export.py).

History: the email path used to PRETEND to send (job completed, nothing
emailed). It now really sends via Postmark — reusing the attachment-capable
``api.session_emails._postmark_send`` helper that powers the per-meeting
Email action — and reports the outcome honestly on the job (``emailSent`` /
``emailError``). When Postmark is NOT configured the request is refused up
front with 501; success is NEVER faked in any path.

Coverage:
  - emailTo + unconfigured Postmark  -> 501 at job creation.
  - emailTo + configured + bogus address -> 422.
  - emailTo + configured + Postmark 200 -> job completed, emailSent=True,
    one base64 attachment with the right name/content-type/content.
  - emailTo + configured + Postmark failure -> job stays completed (the file
    is still downloadable) but emailSent=False + emailError says why.
  - no emailTo -> normal export unaffected, email fields stay None.
"""
import base64
import uuid
from unittest.mock import patch

import pytest

POSTMARK_ENV = {
    "POSTMARK_SERVER_TOKEN": "test-postmark-token",
    "POSTMARK_FROM": "meetings@magicunicorn.dev",
}


def _admin_headers(client):
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_admin_org_session(title: str) -> str:
    """Seed a RecordingSession into the conftest admin's org (magic-unicorn)
    and return its public session_id. The batch endpoint resolves sessions by
    the caller's ACTIVE organization, so the org id must match."""
    import json as _json

    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "magic-unicorn").one()
        admin = db.query(User).filter(User.username == "admin").one()
        sess = RecordingSession(
            session_id=str(uuid.uuid4()),
            name=title,
            title=title,
            status="completed",
            organization_id=org.id,
            user_id=admin.id,
            transcript=_json.dumps({
                "segments": [
                    {"text": f"{title} opening line.", "start": 0.0, "end": 2.0,
                     "speaker": "Alice"},
                ],
            }),
            transcript_simple=f"{title} opening line.",
            final_summary={
                "executive": f"{title} executive summary.",
                "bullets": [f"{title} bullet"],
                "actions": [],
                "decisions": [],
            },
            duration=120.0,
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)
        return sess.session_id
    finally:
        db.close()


def _batch_payload(session_id: str, email_to=None):
    options = {"includeTimestamps": True, "includeSpeakers": True}
    if email_to is not None:
        options["emailTo"] = email_to
    return {"sessionIds": [session_id], "format": "txt", "options": options}


def _clear_postmark_env(monkeypatch):
    for var in ("POSTMARK_SERVER_TOKEN", "POSTMARK_FROM",
                "POSTMARK_API_TOKEN", "POSTMARK_FROM_EMAIL"):
        monkeypatch.delenv(var, raising=False)


def test_email_export_501_when_postmark_unconfigured(client, monkeypatch):
    """No Postmark config -> refuse the emailTo job up front. Never accept
    work we know we can't deliver."""
    _clear_postmark_env(monkeypatch)
    session_id = _seed_admin_org_session("Unconfigured Email Export")
    response = client.post(
        "/api/export/batch",
        json=_batch_payload(session_id, email_to="someone@example.com"),
        headers=_admin_headers(client),
    )
    assert response.status_code == 501, response.text
    assert "not" in response.json()["detail"].lower()


def test_email_export_invalid_address_rejected(client, monkeypatch):
    for key, value in POSTMARK_ENV.items():
        monkeypatch.setenv(key, value)
    session_id = _seed_admin_org_session("Bad Address Export")
    response = client.post(
        "/api/export/batch",
        json=_batch_payload(session_id, email_to="not-an-email"),
        headers=_admin_headers(client),
    )
    assert response.status_code == 422, response.text


def test_email_export_sends_real_email_with_attachment(client, monkeypatch):
    """Happy path: Postmark configured + accepts -> the job records
    emailSent=True and the send carried the export as a base64 attachment."""
    for key, value in POSTMARK_ENV.items():
        monkeypatch.setenv(key, value)
    title = "Emailed Export Meeting"
    session_id = _seed_admin_org_session(title)

    calls = []

    def fake_postmark_send(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "message_id": "pm-test-123"}

    with patch("api.session_emails._postmark_send", side_effect=fake_postmark_send):
        response = client.post(
            "/api/export/batch",
            json=_batch_payload(session_id, email_to="aaron@example.com"),
            headers=_admin_headers(client),
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]

        # TestClient runs BackgroundTasks before the request returns, so the
        # job is already processed here.
        job = client.get(
            f"/api/export/jobs/{job_id}", headers=_admin_headers(client),
        ).json()

    assert job["status"] == "completed", job
    assert job["emailSent"] is True
    assert job["emailError"] is None
    assert job["downloadUrl"]  # download stays available alongside the email

    assert len(calls) == 1, f"expected exactly one Postmark send, got {len(calls)}"
    sent = calls[0]
    assert sent["to_email"] == "aaron@example.com"
    attachments = sent["attachments"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["Name"].endswith(".txt")
    assert attachment["ContentType"] == "text/plain"
    decoded = base64.b64decode(attachment["Content"]).decode("utf-8")
    assert title in decoded
    assert "opening line." in decoded


def test_email_export_failure_reported_honestly(client, monkeypatch):
    """Postmark rejects -> job stays completed (the file exists and is
    downloadable) but emailSent=False and emailError carries the reason.
    No fake success, ever."""
    for key, value in POSTMARK_ENV.items():
        monkeypatch.setenv(key, value)
    session_id = _seed_admin_org_session("Failed Email Export")

    with patch(
        "api.session_emails._postmark_send",
        return_value={"ok": False, "error": "boom: signature not verified"},
    ):
        response = client.post(
            "/api/export/batch",
            json=_batch_payload(session_id, email_to="aaron@example.com"),
            headers=_admin_headers(client),
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        job = client.get(
            f"/api/export/jobs/{job_id}", headers=_admin_headers(client),
        ).json()

    assert job["status"] == "completed", job
    assert job["emailSent"] is False
    assert "boom" in (job["emailError"] or "")
    assert job["downloadUrl"]


def test_batch_export_without_email_unaffected(client, monkeypatch):
    """Plain (no emailTo) batch export must work with or without Postmark
    config, and the email outcome fields stay unset."""
    _clear_postmark_env(monkeypatch)
    session_id = _seed_admin_org_session("Plain Batch Export")
    response = client.post(
        "/api/export/batch",
        json=_batch_payload(session_id),
        headers=_admin_headers(client),
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    job = client.get(
        f"/api/export/jobs/{job_id}", headers=_admin_headers(client),
    ).json()
    assert job["status"] == "completed", job
    assert job["emailSent"] is None
    assert job["emailError"] is None
