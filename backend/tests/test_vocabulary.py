"""Tests for vocabulary CRUD endpoints

NOTE: The vocabulary models use PostgreSQL-specific column types (UUID, ARRAY, JSONB)
which are not supported by SQLite. These tests are skipped in the SQLite test environment.
"""
import os
import pytest


pytestmark = pytest.mark.skipif(
    "sqlite" in os.environ.get("DATABASE_URL", ""),
    reason="Vocabulary models require PostgreSQL (UUID, ARRAY, JSONB column types)",
)


def _get_token(client):
    """Helper to log in and return an access token."""
    response = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    return response.json()["access_token"]


def test_get_terms_empty(client):
    """GET /api/vocabulary/terms returns an empty list when no terms exist."""
    response = client.get("/api/vocabulary/terms")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    assert "total" in data


def test_create_term(client):
    """POST /api/vocabulary/terms creates a term (requires auth)."""
    token = _get_token(client)
    response = client.post(
        "/api/vocabulary/terms",
        json={
            "term": "NPU",
            "expansion": "Neural Processing Unit",
            "category": "hardware",
            "priority": 5,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["term"] == "NPU"
    assert data["expansion"] == "Neural Processing Unit"


def test_get_sets_empty(client):
    """GET /api/vocabulary/sets returns an empty list when no sets exist."""
    response = client.get("/api/vocabulary/sets")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
