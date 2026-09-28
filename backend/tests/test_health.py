"""Tests for health and status endpoints"""


def test_health_endpoint(client):
    """GET /health returns 200 with status field."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert data["status"] in ("healthy", "degraded")


def test_health_has_router_info(client):
    """Health response includes routers_loaded count."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "routers_loaded" in data
    assert isinstance(data["routers_loaded"], int)
    assert data["routers_loaded"] >= 3  # At least the 3 required routers


def test_status_endpoint(client):
    """GET /api/status returns 200 with system info."""
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
