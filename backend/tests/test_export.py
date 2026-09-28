"""Tests for export/download endpoints"""
import uuid

import pytest

from auth.utils import get_password_hash


def _admin_headers(client):
    """The conftest seeds an admin user; we use it for the simple legacy
    smoke tests below. These three tests were authored before auth was
    enforced on the POST /recording-sessions endpoint and silently failed
    with 401; logging in restores the original intent."""
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_session(client, name="Export Test Session"):
    """Helper to create a session and return its id."""
    response = client.post(
        "/api/simple/recording-sessions",
        json={"name": name, "description": "Session for export tests"},
        headers=_admin_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_export_txt(client):
    """GET /download/transcript returns text content for an existing session."""
    session_id = _create_session(client)
    response = client.get(
        f"/api/simple/recording-sessions/{session_id}/download/transcript",
        headers=_admin_headers(client),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # The transcript should contain the session name
    assert "Export Test Session" in response.text


def test_export_transcript_simple(client):
    """GET /download/transcript/simple returns simple text for an existing session."""
    session_id = _create_session(client, name="Simple Export Session")
    response = client.get(
        f"/api/simple/recording-sessions/{session_id}/download/transcript/simple",
        headers=_admin_headers(client),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Simple Export Session" in response.text


def test_export_session_not_found(client):
    """GET /download/transcript returns 404 for a nonexistent session."""
    fake_id = str(uuid.uuid4())
    response = client.get(
        f"/api/simple/recording-sessions/{fake_id}/download/transcript",
        headers=_admin_headers(client),
    )
    assert response.status_code == 404


def test_workspace_report_branding_round_trip_and_default_render(client):
    """Admins can configure a bounded white-label lockup and the saved
    workspace default is honored by both PDF and Word report downloads."""
    headers = _admin_headers(client)
    one_pixel_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
        "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    update = client.put(
        "/api/export/branding",
        headers=headers,
        json={
            "display_name": "Acme Advisory",
            "accent_color": "#126E82",
            "default_mode": "workspace",
            "logo_data_uri": one_pixel_png,
        },
    )
    assert update.status_code == 200, update.text
    assert update.json() == {
        "display_name": "Acme Advisory",
        "accent_color": "#126E82",
        "default_mode": "workspace",
        "has_logo": True,
        "logo_data_uri": one_pixel_png,
    }

    read = client.get("/api/export/branding", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["default_mode"] == "workspace"

    session_id = _create_session(client, name="White Label Export")
    for fmt in ("pdf", "docx"):
        report = client.get(
            f"/api/simple/recording-sessions/{session_id}/download/summary/{fmt}",
            headers=headers,
        )
        assert report.status_code == 200, report.text
        assert len(report.content) > 500


def test_report_branding_rejects_non_image_data(client):
    response = client.put(
        "/api/export/branding",
        headers=_admin_headers(client),
        json={
            "logo_data_uri": (
                "data:image/png;base64,"
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
            )
        },
    )
    assert response.status_code == 422
    assert "decode" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /api/recording-sessions/{id}/export — meeting_management endpoint
#
# Regression coverage for two long-standing bugs (fixed 2026-05-20):
#   1. Markdown was missing from the Query() regex, so any caller asking for
#      ?format=md|markdown got an FastAPI-422 before the handler ran.
#   2. The handler filtered by ``RecordingSession.user_id`` which is legacy
#      pre-multi-org state; the canonical filter is ``organization_id``. The
#      result was 404 for every valid session on freshly-created data.
# ---------------------------------------------------------------------------


def _seed_org_and_user(suffix: str):
    """Create a fresh org + user + membership.

    Returns plain ints/strings (``org_id``, ``user_id``, ``username``) so the
    caller never touches detached ORM instances after the session closes —
    SQLAlchemy raises DetachedInstanceError on lazy-loaded attrs once a
    Session is closed, which is exactly what happens in this helper.
    """
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        org = Organization(
            name=f"Export Org {suffix}",
            slug=f"export-{suffix}",
            is_active=True,
        )
        user = User(
            email=f"export_{suffix}@example.com",
            username=f"export_user_{suffix}",
            hashed_password=get_password_hash("Password123"),
            is_active=True,
            is_verified=True,
        )
        db.add_all([org, user])
        db.commit()
        db.refresh(org)
        db.refresh(user)
        db.add(UserOrganization(
            user_id=user.id, organization_id=org.id, role="user",
        ))
        db.commit()
        # Snapshot the values before the session closes — afterwards the
        # ORM instances are detached and attribute access raises.
        return {
            "org_id": org.id,
            "user_id": user.id,
            "username": user.username,
        }
    finally:
        db.close()


def _seed_session(*, organization_id: int, user_id: int, title: str):
    """Drop a populated RecordingSession into the DB and return its public
    ``session_id`` (UUID string) and integer PK."""
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        sess = RecordingSession(
            session_id=str(uuid.uuid4()),
            name=title,
            title=title,
            status="completed",
            organization_id=organization_id,
            user_id=user_id,
            transcript_simple=f"{title} transcript body.",
            transcript=f"{title} transcript body.",
            summary=f"{title} summary.",
            final_summary={
                "executive": f"{title} executive summary line.",
                "bullets": [f"{title} bullet one", f"{title} bullet two"],
                "actions": [{"action": f"{title} action item", "owner": "alice"}],
                "decisions": [f"{title} decision."],
            },
            duration=180.0,
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)
        return {"public_id": sess.session_id, "db_id": sess.id}
    finally:
        db.close()


def _login_headers(client, username: str, password: str = "Password123") -> dict:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def two_orgs_with_sessions(client):
    """Seed org A + org B, one user + one session each. Return a dict with
    auth headers for org A and the public session_ids for both orgs."""
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    a = _seed_org_and_user(suffix_a)
    b = _seed_org_and_user(suffix_b)
    sess_a = _seed_session(
        organization_id=a["org_id"], user_id=a["user_id"], title="Alpha Export Meeting",
    )
    sess_b = _seed_session(
        organization_id=b["org_id"], user_id=b["user_id"], title="Bravo Export Meeting",
    )
    return {
        "headers_a": _login_headers(client, a["username"]),
        "sess_a_id": sess_a["public_id"],
        "sess_b_id": sess_b["public_id"],
    }


def _seed_null_speaker_transcription(*, session_db_id: int):
    """Insert a Transcription row whose speaker is NULL — the case that made the
    legacy txt/json/srt export 500 via the nonexistent ``t.speaker_id``."""
    from database.database import SessionLocal
    from database.models import Transcription

    db = SessionLocal()
    try:
        db.add(Transcription(
            session_id=session_db_id, text="unattributed line",
            speaker=None, start_time=0.0, end_time=1.5, confidence=0.9,
        ))
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("fmt", ["txt", "json", "srt"])
def test_export_null_speaker_does_not_500(client, fmt):
    """A transcript segment with speaker=NULL must export cleanly. Pre-fix the
    fallback referenced Transcription.speaker_id (no such column) -> 500."""
    suffix = uuid.uuid4().hex[:6]
    seeded = _seed_org_and_user(suffix)
    sess = _seed_session(
        organization_id=seeded["org_id"], user_id=seeded["user_id"],
        title="Null Speaker Meeting",
    )
    _seed_null_speaker_transcription(session_db_id=sess["db_id"])
    headers = _login_headers(client, seeded["username"])

    resp = client.get(
        f"/api/recording-sessions/{sess['public_id']}/export",
        params={"format": fmt}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    if fmt in ("txt", "srt"):
        assert "Speaker 1" in resp.text  # deterministic fallback label
    else:  # json
        assert resp.json()["transcript"][0]["speaker"] == "Speaker 1"


@pytest.mark.parametrize("fmt", ["md", "markdown"])
def test_export_markdown_accepted(client, two_orgs_with_sessions, fmt):
    """GET /api/recording-sessions/{id}/export?format=md|markdown returns 200
    with text/markdown content. Pre-fix this 422'd at the Query() regex."""
    response = client.get(
        f"/api/recording-sessions/{two_orgs_with_sessions['sess_a_id']}/export",
        params={"format": fmt},
        headers=two_orgs_with_sessions["headers_a"],
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    body = response.text
    # Markdown rendering must surface the title + executive summary.
    assert "Alpha Export Meeting" in body
    assert "executive summary line" in body.lower()


def test_export_markdown_cross_org_returns_404(client, two_orgs_with_sessions):
    """Exporting org B's session as org A must 404 — not return org B's data."""
    response = client.get(
        f"/api/recording-sessions/{two_orgs_with_sessions['sess_b_id']}/export",
        params={"format": "markdown"},
        headers=two_orgs_with_sessions["headers_a"],
    )
    assert response.status_code == 404, (
        f"Leak: org A got status {response.status_code} for org B's session. "
        f"Body: {response.text[:300]}"
    )


@pytest.mark.parametrize("fmt,expected_ct", [
    ("txt", "text/plain"),
    ("json", "application/json"),
    ("srt", "text/plain"),
])
def test_export_other_formats_still_work(client, two_orgs_with_sessions, fmt, expected_ct):
    """Regression: txt/json/srt must keep working after the markdown +
    org-scoping rewrite."""
    response = client.get(
        f"/api/recording-sessions/{two_orgs_with_sessions['sess_a_id']}/export",
        params={"format": fmt},
        headers=two_orgs_with_sessions["headers_a"],
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(expected_ct), (
        f"Format {fmt} returned content-type "
        f"{response.headers['content-type']!r}; expected {expected_ct!r}"
    )


def test_export_invalid_format_rejected(client, two_orgs_with_sessions):
    """Unknown formats (e.g. ?format=html) must 422 at the Query() regex."""
    response = client.get(
        f"/api/recording-sessions/{two_orgs_with_sessions['sess_a_id']}/export",
        params={"format": "html"},
        headers=two_orgs_with_sessions["headers_a"],
    )
    assert response.status_code == 422, (
        f"Expected 422 for unknown format, got {response.status_code}: {response.text}"
    )


def test_download_summary_md_includes_transcript_option(client, two_orgs_with_sessions):
    """Regression: ``ExportOptions(includeTranscript=...)`` used to silently
    drop the kwarg (pydantic v2 ignores unknown fields), then crash with
    AttributeError when ``export_to_markdown`` read it. Confirms the
    /download/summary/md path now resolves cleanly with both defaults and
    explicit include_transcript=true."""
    # Default (include_transcript=False) — must succeed.
    response = client.get(
        f"/api/simple/recording-sessions/{two_orgs_with_sessions['sess_a_id']}/download/summary/md",
        headers=two_orgs_with_sessions["headers_a"],
    )
    assert response.status_code == 200, response.text
    assert "Alpha Export Meeting" in response.text

    # Explicit include_transcript=true — must also succeed and include the
    # transcript body line ("Alpha Export Meeting transcript body.").
    response = client.get(
        f"/api/simple/recording-sessions/{two_orgs_with_sessions['sess_a_id']}/download/summary/md",
        params={"include_transcript": "true"},
        headers=two_orgs_with_sessions["headers_a"],
    )
    assert response.status_code == 200, response.text
    assert "Alpha Export Meeting transcript body." in response.text


@pytest.mark.parametrize("renderer_name", ["export_to_pdf", "export_to_docx"])
def test_branded_report_renderers_do_not_read_transcript_when_excluded(
    monkeypatch, renderer_name
):
    """Summary reports default to the share-safe form. The raw transcript
    helpers must not even be touched unless the caller explicitly opts in."""
    from types import SimpleNamespace
    from api import batch_export

    monkeypatch.setattr(
        batch_export,
        "_get_summary_data",
        lambda _session: {
            "title": "Board Review",
            "executive": "A concise executive brief.",
            "bullets": ["Revenue grew."],
            "actions": [{"action": "Send forecast", "owner": "Alex"}],
            "decisions": ["Proceed with launch."],
            "participants": ["Alex", "Taylor"],
        },
    )

    def _unexpected(_session):
        raise AssertionError("transcript helper must not run")

    monkeypatch.setattr(batch_export, "_get_transcript_segments", _unexpected)
    monkeypatch.setattr(batch_export, "_get_plain_transcript", _unexpected)
    session = SimpleNamespace(
        meeting_date=None,
        meeting_time=None,
        started_at=None,
        created_at=None,
        duration=95,
    )

    output = getattr(batch_export, renderer_name)(
        session,
        batch_export.ExportOptions(includeTranscript=False),
    )
    assert len(output) > 500


@pytest.mark.parametrize("renderer_name", ["export_to_pdf", "export_to_docx"])
def test_branded_report_renderers_read_transcript_only_when_included(
    monkeypatch, renderer_name
):
    from types import SimpleNamespace
    from api import batch_export

    calls = {"segments": 0, "plain": 0}
    monkeypatch.setattr(
        batch_export,
        "_get_summary_data",
        lambda _session: {
            "title": "Board Review",
            "executive": "",
            "bullets": [],
            "actions": [],
            "decisions": [],
            "participants": [],
        },
    )

    def _segments(_session):
        calls["segments"] += 1
        return [
            {
                "speaker": "Alex",
                "text": "This line belongs only in the appendix.",
                "start": 2.0,
            }
        ]

    def _plain(_session):
        calls["plain"] += 1
        return ""

    monkeypatch.setattr(batch_export, "_get_transcript_segments", _segments)
    monkeypatch.setattr(batch_export, "_get_plain_transcript", _plain)
    session = SimpleNamespace(
        meeting_date=None,
        meeting_time=None,
        started_at=None,
        created_at=None,
        duration=95,
    )

    output = getattr(batch_export, renderer_name)(
        session,
        batch_export.ExportOptions(includeTranscript=True),
    )
    assert len(output) > 500
    assert calls == {"segments": 1, "plain": 1}
