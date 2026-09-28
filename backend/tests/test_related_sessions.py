"""Same-meeting duplicate DETECTION (v3.36.0, v1 = detect + link, no merge).

Workspaces are shared: two members can record the SAME call, producing two
sessions of the same meeting. `_related_sessions` in api/simple_recording_db.py
computes — on read, no migration/stamping — the OTHER completed sessions in
the SAME organization by a DIFFERENT user whose recording window overlaps
this one's by more than 50% of the SHORTER session, and the session DETAIL
payload surfaces them as "related_sessions".

Covers: >50% overlap links (both directions), same-user excluded,
cross-ORG excluded (tenant wall — the whole point), <50% overlap excluded,
sub-60s sessions excluded, null duration excluded without raising, and the
HTTP detail-payload contract shape.

Seed style mirrors test_summary_idempotency.py / test_cross_org_isolation.py:
client fixture + lazy model imports (auth.models first so the users FK
resolves on the SQLite fixture).
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta

from auth.utils import get_password_hash

# Far-past anchor so no other test's sessions land inside the +/- 24h coarse
# window (we also use fresh orgs, which is the real isolation).
T0 = datetime(2026, 1, 15, 10, 0, 0)


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_org(db, suffix: str, label: str):
    Organization, _, _, _, _ = _models()
    org = Organization(
        name=f"Rel {label} {suffix}",
        slug=f"rel-{label}-{suffix}",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _seed_user(db, org_id: int, username: str, full_name: str):
    _, User, UserOrganization, _, _ = _models()
    user = User(
        email=f"{username}@meeting-ops.local",
        username=username,
        full_name=full_name,
        hashed_password=get_password_hash("Password123"),
        is_active=True,
        is_verified=True,
        tier="enterprise",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(UserOrganization(user_id=user.id, organization_id=org_id, role="user"))
    db.commit()
    return user


def _make_session(
    db,
    *,
    org_id: int,
    user_id: int,
    name: str,
    started_at: datetime | None,
    duration: float | None,
    status: str = "completed",
):
    _, _, _, _, RecordingSession = _models()
    sess = RecordingSession(
        session_id=str(_uuid.uuid4()),
        name=name,
        title=name,
        status=status,
        organization_id=org_id,
        user_id=user_id,
        started_at=started_at,
        duration=duration,
        transcript_simple=f"transcript for {name}",
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def _seed_world(db, suffix: str):
    """Org X with users A + B, org Y with user C."""
    org_x = _seed_org(db, suffix, "x")
    org_y = _seed_org(db, suffix, "y")
    user_a = _seed_user(db, org_x.id, f"rel_a_{suffix}", "Alice Recorder")
    user_b = _seed_user(db, org_x.id, f"rel_b_{suffix}", "Bob Recorder")
    user_c = _seed_user(db, org_y.id, f"rel_c_{suffix}", "Carol Outsider")
    return org_x, org_y, user_a, user_b, user_c


def _related_ids(db, session) -> list[int]:
    from api.simple_recording_db import _related_sessions

    return [r["id"] for r in _related_sessions(db, session)]


def test_overlap_links_both_directions_and_walls_hold(client):
    """One 30-min call recorded by A and B (B starts 5 min late -> 25 min
    overlap = 83% of the shorter session): each side surfaces the other.
    Same-user, cross-org, <50%-overlap, sub-60s, and null-duration
    candidates in the SAME window are all excluded."""
    from api.simple_recording_db import _related_sessions

    _, _, _, SessionLocal, _ = _models()
    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, org_y, user_a, user_b, user_c = _seed_world(db, suffix)

        # The real duplicate pair.
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Standup (Alice)",
            started_at=T0, duration=1800.0,
        )
        sess_b = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Standup (Bob)",
            started_at=T0 + timedelta(minutes=5), duration=1800.0,
        )
        # Same user as A, fully overlapping -> excluded (provenance split,
        # not two people on one call).
        sess_a2 = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Standup (Alice dup)",
            started_at=T0, duration=1800.0,
        )
        # Identical window but in org Y -> NEVER links (tenant wall).
        sess_c = _make_session(
            db, org_id=org_y.id, user_id=user_c.id, name="Standup (Carol)",
            started_at=T0, duration=1800.0,
        )
        # Different user, same org, but only 5/30 min overlap (17% < 50%).
        sess_tail = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Tail meeting",
            started_at=T0 + timedelta(minutes=25), duration=1800.0,
        )
        # Different user, inside A's window, but sub-60s -> excluded.
        sess_blip = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Blip",
            started_at=T0 + timedelta(minutes=1), duration=45.0,
        )
        # Different user, inside A's window, null duration -> excluded,
        # and detection must not raise.
        sess_nodur = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="No duration",
            started_at=T0 + timedelta(minutes=1), duration=None,
        )

        related_a = _related_sessions(db, sess_a)
        ids_a = [r["id"] for r in related_a]
        assert sess_b.id in ids_a
        assert sess_a2.id not in ids_a          # same user
        assert sess_c.id not in ids_a           # cross-org
        assert sess_tail.id not in ids_a        # <50% overlap
        assert sess_blip.id not in ids_a        # sub-60s
        assert sess_nodur.id not in ids_a       # null duration, no exception
        assert ids_a == [sess_b.id]

        # Contract shape + recorded_by reuse.
        entry = related_a[0]
        assert set(entry.keys()) == {
            "id", "name", "recorded_by", "started_at", "duration",
        }
        assert entry["name"] == "Standup (Bob)"
        assert entry["recorded_by"] == "Bob Recorder"
        assert entry["duration"] == 1800.0
        assert entry["started_at"] == (T0 + timedelta(minutes=5)).isoformat()

        # Symmetric: B's detail links back to A (overlap is 1500s > 50% of
        # the shorter 1800s session in both directions).
        ids_b = _related_ids(db, sess_b)
        assert sess_a.id in ids_b
        assert sess_a2.id in ids_b  # A's dup is a different user than B
        assert sess_c.id not in ids_b
    finally:
        db.close()


def test_exact_50_percent_overlap_is_excluded(client):
    """The contract is STRICTLY more than 50% of the shorter session."""
    _, _, _, SessionLocal, _ = _models()
    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, _, user_a, user_b, _ = _seed_world(db, suffix)
        # A: 30 min. B: 20 min starting 20 min in -> overlap 600s == 50%
        # of B's 1200s exactly -> excluded.
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Half A",
            started_at=T0, duration=1800.0,
        )
        sess_b = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Half B",
            started_at=T0 + timedelta(minutes=20), duration=1200.0,
        )
        assert _related_ids(db, sess_a) == []
        assert _related_ids(db, sess_b) == []
    finally:
        db.close()


def test_non_completed_candidates_excluded(client):
    _, _, _, SessionLocal, _ = _models()
    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, _, user_a, user_b, _ = _seed_world(db, suffix)
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Done",
            started_at=T0, duration=1800.0,
        )
        _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Still processing",
            started_at=T0, duration=1800.0, status="processing",
        )
        assert _related_ids(db, sess_a) == []
    finally:
        db.close()


def test_base_session_with_null_duration_returns_empty(client):
    """A session whose OWN window can't be computed yields [] (no raise)."""
    _, _, _, SessionLocal, _ = _models()
    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, _, user_a, user_b, _ = _seed_world(db, suffix)
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="No own duration",
            started_at=T0, duration=None,
        )
        _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Fine candidate",
            started_at=T0, duration=1800.0,
        )
        assert _related_ids(db, sess_a) == []
    finally:
        db.close()


def test_created_at_fallback_when_started_at_missing(client):
    """started_at null -> the window falls back to created_at; two
    same-created-at copies still link."""
    _, _, _, SessionLocal, RecordingSession = _models()
    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, _, user_a, user_b, _ = _seed_world(db, suffix)
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Fallback A",
            started_at=None, duration=1800.0,
        )
        sess_b = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Fallback B",
            started_at=None, duration=1800.0,
        )
        # Pin created_at to the same anchor (insert default is "now"; the
        # two inserts are already within ms, but make it deterministic).
        for pk in (sess_a.id, sess_b.id):
            row = db.query(RecordingSession).filter(RecordingSession.id == pk).first()
            row.created_at = T0
        db.commit()
        db.refresh(sess_a)
        db.refresh(sess_b)

        assert _related_ids(db, sess_a) == [sess_b.id]
        assert _related_ids(db, sess_b) == [sess_a.id]
    finally:
        db.close()


def test_detail_endpoint_surfaces_related_sessions(client):
    """HTTP wiring: GET single session carries "related_sessions" per the
    cross-agent contract — and an unrelated session carries an empty list."""
    _, _, _, SessionLocal, _ = _models()
    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, org_y, user_a, user_b, user_c = _seed_world(db, suffix)
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="HTTP Standup (Alice)",
            started_at=T0, duration=1800.0,
        )
        sess_b = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="HTTP Standup (Bob)",
            started_at=T0 + timedelta(minutes=5), duration=1800.0,
        )
        # Cross-org twin: must not appear for A.
        _make_session(
            db, org_id=org_y.id, user_id=user_c.id, name="HTTP Standup (Carol)",
            started_at=T0, duration=1800.0,
        )
        # A lone session far away -> empty related list on its detail.
        sess_lone = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Lone session",
            started_at=T0 + timedelta(days=10), duration=1800.0,
        )
        a_public_id = sess_a.session_id
        lone_public_id = sess_lone.session_id
        b_pk, b_name = sess_b.id, sess_b.name
        a_username = user_a.username
    finally:
        db.close()

    login = client.post(
        "/api/auth/login",
        data={"username": a_username, "password": "Password123"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.get(f"/api/simple/recording-sessions/{a_public_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    related = resp.json()["related_sessions"]
    assert isinstance(related, list)
    assert [r["id"] for r in related] == [b_pk]
    entry = related[0]
    assert entry["name"] == b_name
    assert entry["recorded_by"] == "Bob Recorder"
    assert entry["duration"] == 1800.0
    assert entry["started_at"] == (T0 + timedelta(minutes=5)).isoformat()

    resp = client.get(
        f"/api/simple/recording-sessions/{lone_public_id}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["related_sessions"] == []


def test_collaborator_fallback_caller_gets_empty_related_sessions(client):
    """v3.36.0 hardening: an EXTERNAL per-meeting collaborator (org-Y user
    invited to one org-X session, resolved via the has_session_access
    fallback in get_session) can read the shared session but must NOT see
    the host org's OTHER sessions — related_sessions is empty for them,
    while an actual org member still sees the duplicate."""
    _, _, _, SessionLocal, _ = _models()
    from database.models import SessionCollaborator

    suffix = _uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        org_x, _, user_a, user_b, user_c = _seed_world(db, suffix)
        # A real duplicate pair inside org X.
        sess_a = _make_session(
            db, org_id=org_x.id, user_id=user_a.id, name="Collab Standup (Alice)",
            started_at=T0, duration=1800.0,
        )
        sess_b = _make_session(
            db, org_id=org_x.id, user_id=user_b.id, name="Collab Standup (Bob)",
            started_at=T0 + timedelta(minutes=5), duration=1800.0,
        )
        # Carol (org Y only) is an explicit collaborator on Alice's session.
        db.add(SessionCollaborator(
            session_id=sess_a.id,
            user_id=user_c.id,
            email=user_c.email,
            access_level="read",
            invited_by_user_id=user_a.id,
        ))
        db.commit()
        a_public_id = sess_a.session_id
        b_pk = sess_b.id
        a_username, c_username = user_a.username, user_c.username
    finally:
        db.close()

    def _headers(username: str) -> dict:
        login = client.post(
            "/api/auth/login",
            data={"username": username, "password": "Password123"},
        )
        assert login.status_code == 200, login.text
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    # The org member sees the duplicate.
    resp = client.get(
        f"/api/simple/recording-sessions/{a_public_id}", headers=_headers(a_username)
    )
    assert resp.status_code == 200, resp.text
    assert [r["id"] for r in resp.json()["related_sessions"]] == [b_pk]

    # The external collaborator can read the session (fallback path) but
    # gets NO related-session metadata from the host org.
    resp = client.get(
        f"/api/simple/recording-sessions/{a_public_id}", headers=_headers(c_username)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["related_sessions"] == []
