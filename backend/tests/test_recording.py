"""Tests for recording session CRUD endpoints"""
import uuid
from unittest.mock import AsyncMock, patch


def _auth_headers(client):
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_create_session(client):
    """POST /api/simple/recording-sessions creates a session and returns its id."""
    response = client.post(
        "/api/simple/recording-sessions",
        json={"name": "Test Session", "description": "Unit test session"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert data["name"] == "Test Session"
    assert data["status"] == "active"


def test_list_sessions(client):
    """GET /api/simple/recording-sessions returns a cursor page."""
    # Create a session first
    client.post(
        "/api/simple/recording-sessions",
        json={"name": "List Test Session"},
        headers=_auth_headers(client),
    )
    response = client.get("/api/simple/recording-sessions", headers=_auth_headers(client))
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 1
    assert "transcription" not in data["items"][0]


def test_list_sessions_cursor_has_no_duplicates(client):
    headers = _auth_headers(client)
    for index in range(3):
        client.post(
            "/api/simple/recording-sessions",
            json={"name": f"Cursor Session {index}"},
            headers=headers,
        )

    first = client.get("/api/simple/recording-sessions?limit=2", headers=headers).json()
    assert first["next_cursor"]
    second = client.get(
        f"/api/simple/recording-sessions?limit=2&cursor={first['next_cursor']}",
        headers=headers,
    ).json()
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_get_session_detail(client):
    """GET /{id} returns full session data with expected fields."""
    create_response = client.post(
        "/api/simple/recording-sessions",
        json={"name": "Detail Test Session", "description": "details"},
        headers=_auth_headers(client),
    )
    session_id = create_response.json()["id"]

    response = client.get(f"/api/simple/recording-sessions/{session_id}", headers=_auth_headers(client))
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == session_id
    assert data["name"] == "Detail Test Session"


def test_get_session_not_found(client):
    """GET with nonexistent id returns 404."""
    fake_id = str(uuid.uuid4())
    response = client.get(f"/api/simple/recording-sessions/{fake_id}", headers=_auth_headers(client))
    assert response.status_code == 404


def test_delete_session(client):
    """DELETE removes the session."""
    create_response = client.post(
        "/api/simple/recording-sessions",
        json={"name": "Delete Test Session"},
        headers=_auth_headers(client),
    )
    session_id = create_response.json()["id"]

    delete_response = client.delete(f"/api/simple/recording-sessions/{session_id}", headers=_auth_headers(client))
    assert delete_response.status_code == 200

    # Verify it's gone
    get_response = client.get(f"/api/simple/recording-sessions/{session_id}", headers=_auth_headers(client))
    assert get_response.status_code == 404


def test_delete_session_purges_brigade_subgraph(client):
    from database.database import SessionLocal
    from database.models import RecordingSession
    from services.brigade_client import BrigadeWriteResult

    headers = _auth_headers(client)
    created = client.post(
        "/api/simple/recording-sessions",
        json={"name": "Brigade Delete Session"},
        headers=headers,
    ).json()
    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.session_id == created["id"]).first()
        session.brigade_graph_node_id = f"meeting_ops_meeting_{session.id}"
        org_id = session.organization_id
        node_id = session.brigade_graph_node_id
        db.commit()
    finally:
        db.close()

    delete_graph = AsyncMock(return_value=BrigadeWriteResult(ok=True, mode="live"))
    with patch("services.brigade_writer.delete_session_from_brigade", delete_graph):
        response = client.delete(
            f"/api/simple/recording-sessions/{created['id']}",
            headers=headers,
        )

    assert response.status_code == 200
    delete_graph.assert_awaited_once_with(org_id, node_id)


def test_delete_session_not_found(client):
    """DELETE with nonexistent id returns 404."""
    fake_id = str(uuid.uuid4())
    response = client.delete(f"/api/simple/recording-sessions/{fake_id}", headers=_auth_headers(client))
    assert response.status_code == 404


def test_create_session_without_name(client):
    """POST without the required 'name' field returns 422 validation error."""
    response = client.post(
        "/api/simple/recording-sessions",
        json={"description": "No name provided"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 422


def test_session_fields(client):
    """Verify response has expected fields: id, name, status, created_at, duration."""
    response = client.post(
        "/api/simple/recording-sessions",
        json={"name": "Fields Test Session", "description": "Check fields"},
        headers=_auth_headers(client),
    )
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert "name" in data
    assert "status" in data
    assert "created_at" in data
    assert "duration" in data
