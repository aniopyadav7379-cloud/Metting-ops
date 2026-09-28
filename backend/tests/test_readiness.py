from unittest.mock import AsyncMock, patch


def test_readiness_returns_200_when_dependencies_are_up(client):
    payload = {
        "status": "ready",
        "ready": True,
        "dependencies": {"database": {"status": "ok", "ok": True}},
    }
    with patch(
        "services.readiness.run_readiness_checks",
        AsyncMock(return_value=payload),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_readiness_returns_503_when_dependency_is_down(client):
    payload = {
        "status": "degraded",
        "ready": False,
        "dependencies": {
            "database": {"status": "down", "ok": False, "error": "unreachable"},
        },
    }
    with patch(
        "services.readiness.run_readiness_checks",
        AsyncMock(return_value=payload),
    ):
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["dependencies"]["database"]["status"] == "down"
