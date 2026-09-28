"""Tests for authentication endpoints"""
import jwt


def _get_token(client):
    """Helper to log in and return access token."""
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    return response.json()["access_token"]


def test_login_success(client):
    """Valid credentials return a token and user info."""
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["user"]["username"] == "admin"
    assert data["token_type"] == "bearer"


def test_login_invalid_password(client):
    """Wrong password returns 401."""
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "wrongpassword"},
    )
    assert response.status_code == 401
    data = response.json()
    assert "detail" in data


def test_login_invalid_username(client):
    """Nonexistent user returns 401."""
    response = client.post(
        "/api/auth/login",
        data={"username": "nonexistent_user", "password": "admin123"},
    )
    assert response.status_code == 401
    data = response.json()
    assert "detail" in data


def test_protected_route_without_token(client):
    """Accessing /api/auth/me without token returns 401."""
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_protected_route_with_valid_token(client):
    """Accessing /api/auth/me with valid token returns 200 with user data."""
    token = _get_token(client)
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "admin"
    assert data["is_active"] is True


def test_token_format(client):
    """Token is a valid JWT with expected claims."""
    token = _get_token(client)
    # Decode without verification to inspect claims
    payload = jwt.decode(token, options={"verify_signature": False})
    assert "sub" in payload  # subject (user id)
    assert "exp" in payload  # expiration
    assert payload.get("type") == "access"
