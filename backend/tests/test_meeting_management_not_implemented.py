"""meeting_management.py honesty audit (2026-06-10).

The router historically served FAKE data from several endpoints the frontend
never calls (verified: frontend/src has zero references to any route this
module uniquely owns). Those endpoints now return an honest HTTP 501 instead
of fabricated payloads:

  - POST /api/recording-sessions                   (in-memory mock; also
    shadowed by api/sessions.py which loads first — the REAL handler wins)
  - POST /api/recording-sessions/{id}/reprocess    (fake NPU stamps)
  - GET  /api/system/npu-status                    (fabricated 250x metrics)
  - /api/summarization-templates CRUD              (process-local list)

These tests pin the 501 contract AND that the router still loads (so the
/health router count stays stable), AND that the real, kept endpoints were
not collaterally broken (export coverage lives in test_export.py).
"""
import pytest


def _admin_headers(client):
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_npu_status_returns_501(client):
    """The fabricated NPU metrics endpoint must answer 501, not fake data."""
    response = client.get("/api/system/npu-status")
    assert response.status_code == 501
    assert response.json()["detail"] == "Not implemented"


def test_reprocess_returns_501(client):
    """The fake-NPU reprocess endpoint must answer 501 instead of pretending
    to reprocess (it used to stamp npu_processed=True without doing work)."""
    response = client.post(
        "/api/recording-sessions/some-session-id/reprocess",
        headers=_admin_headers(client),
    )
    assert response.status_code == 501
    assert response.json()["detail"] == "Not implemented"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/summarization-templates"),
        ("post", "/api/summarization-templates"),
        ("put", "/api/summarization-templates/some-id"),
        ("delete", "/api/summarization-templates/some-id"),
    ],
)
def test_summarization_templates_return_501(client, method, path):
    """Template CRUD 'persisted' to a process-local list — now honest 501s."""
    response = getattr(client, method)(path, headers=_admin_headers(client))
    assert response.status_code == 501
    assert response.json()["detail"] == "Not implemented"


def test_create_session_route_still_served_by_real_handler(client):
    """POST /api/recording-sessions must keep hitting api/sessions.py's REAL
    creator (loaded before meeting_management): NOT the 501 mock stub."""
    response = client.post(
        "/api/recording-sessions",
        json={"title": "Routing Order Pin"},
        headers=_admin_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Routing Order Pin"
    assert body["status"] == "pending"


def test_meeting_management_router_still_loads(client):
    """501-ing the fake endpoints must not knock the router out of the app —
    /health's router accounting has to stay stable."""
    response = client.get("/health")
    assert response.status_code == 200
    health = response.json()
    assert "meeting_management" not in health.get("routers_failed", [])


def test_analytics_meetings_returns_501(client):
    """GET /api/analytics/meetings read model columns that no longer exist
    (npu_processed, file_size) and 500'd whenever any session existed in
    range — it could never serve real data. Honest 501 now; the real
    analytics live under api/analytics_simple.py."""
    response = client.get(
        "/api/analytics/meetings",
        params={"range": "week"},
        headers=_admin_headers(client),
    )
    assert response.status_code == 501
    assert response.json()["detail"] == "Not implemented"


def test_legacy_transcript_replace_returns_501(client):
    response = client.put(
        "/api/recording-sessions/123/transcript",
        json={"transcript": []},
        headers=_admin_headers(client),
    )
    assert response.status_code == 501
