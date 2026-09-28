"""Tests for the TTS API surface and provider routing.

These exercise the routing/glue layer rather than the upstream services. The
real Kokoro and VibeVoice containers are not assumed to be reachable from the
test runner, so we monkeypatch the provider classes to return fake bytes.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from auth.utils import get_password_hash


def _current_models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession, OrgProviderSettings
    return Organization, User, UserOrganization, SessionLocal, RecordingSession, OrgProviderSettings


def _login_headers(client, username: str, password: str, org_slug: str | None = None) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user(db, username: str, password: str, email: str, is_superuser: bool = False):
    _, User, _, _, _, _ = _current_models()
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


def _seed_org(db, name: str, slug: str):
    Organization, _, _, _, _, _ = _current_models()
    org = Organization(name=name, slug=slug, is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _seed_membership(db, user_id: int, org_id: int, role: str = "user"):
    _, _, UserOrganization, _, _, _ = _current_models()
    mem = UserOrganization(user_id=user_id, organization_id=org_id, role=role)
    db.add(mem)
    db.commit()
    return mem


def _seed_session_with_summary(db, org_id: int, name: str = "Test Meeting"):
    _, _, _, _, RecordingSession, _ = _current_models()
    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=name,
        title=name,
        status="completed",
        organization_id=org_id,
        duration=120.0,
        final_summary={
            "executive": "We aligned on the Q3 roadmap.",
            "bullets": ["Ship Phase 4", "Tighten observability"],
            "actions": [
                {"action": "Draft launch plan", "owner": "Aaron"},
                {"action": "Wire up alerts", "owner": "Shafen"},
            ],
            "decisions": ["Move to Gemma-4 26B-MoE"],
        },
        transcript_simple="Hello and welcome.",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@pytest.fixture(autouse=True)
def _isolate_recordings_dir(tmp_path, monkeypatch):
    """Redirect tts output away from /app/recordings during tests."""
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))
    # Reset module-level constant if api.tts is already imported in a session.
    import importlib
    import api.tts as tts_module
    importlib.reload(tts_module)
    yield


class _FakeTTSProvider:
    """Minimal fake — the registry returns instances of this so we don't hit
    the network. Behaviour is configurable via class-level toggles."""
    name = "fake"
    supports_podcast = True

    async def synthesize(self, text: str, *, voice=None, format="mp3") -> bytes:
        return b"FAKE-" + format.encode() + b"-" + text[:32].encode()

    async def synthesize_podcast(self, script, voices, *, format="mp3") -> bytes:
        return b"FAKE-PODCAST-" + format.encode() + b"-" + str(len(script)).encode()

    async def list_voices(self) -> list[dict]:
        return [{"voice_id": "alice", "label": "alice"}, {"voice_id": "frank", "label": "frank"}]

    async def health(self) -> dict:
        return {"ok": True, "provider": self.name}


class _FakeKokoroProvider(_FakeTTSProvider):
    name = "kokoro"
    supports_podcast = False

    async def synthesize_podcast(self, script, voices, *, format="mp3"):
        raise NotImplementedError("Kokoro has no podcast mode")


class _FakeLLM:
    async def chat(self, system_prompt, user_prompt, *, max_tokens=500, temperature=0.7):
        return json.dumps({
            "script": [
                {"speaker_id": "host", "text": "Welcome to the recap."},
                {"speaker_id": "analyst", "text": "Big news: Phase 4 ships."},
                {"speaker_id": "host", "text": "Thanks for tuning in."},
            ],
        })


class TestTTSVoices:
    """GET /api/tts/voices returns the voices for the active provider."""

    def test_voices_for_default_provider(self, client, monkeypatch):
        from services.providers import registry as registry_module

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "tts_a", "test123", "tts_a@test.com", is_superuser=True)
            org = _seed_org(db, "TTS Org", "tts-org")
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, "tts_a", "test123", "tts-org")

            monkeypatch.setattr(
                registry_module.ProviderRegistry,
                "get_tts",
                lambda self, org_id: _FakeTTSProvider(),
            )

            resp = client.get("/api/tts/voices", headers=headers)
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["provider"] == "fake"
            assert payload["supports_podcast"] is True
            assert any(v["voice_id"] == "alice" for v in payload["voices"])
        finally:
            db.rollback()
            db.close()


class TestTTSummary:
    """POST /api/sessions/{id}/tts/summary."""

    def test_summary_synth_writes_audio(self, client, monkeypatch):
        from services.providers import registry as registry_module

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "tts_b", "test123", "tts_b@test.com", is_superuser=True)
            org = _seed_org(db, "Summary Org", "summary-org")
            _seed_membership(db, user.id, org.id, "admin")
            session = _seed_session_with_summary(db, org.id)
            headers = _login_headers(client, "tts_b", "test123", "summary-org")

            monkeypatch.setattr(
                registry_module.ProviderRegistry,
                "get_tts",
                lambda self, org_id: _FakeTTSProvider(),
            )

            resp = client.post(
                f"/api/sessions/{session.id}/tts/summary?format=mp3",
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == session.id
            assert body["provider"] == "fake"
            assert body["format"] == "mp3"
            assert body["bytes"] > 0
            assert body["cached"] is False

            # Re-call should return cached
            resp2 = client.post(
                f"/api/sessions/{session.id}/tts/summary?format=mp3",
                headers=headers,
            )
            assert resp2.json()["cached"] is True

            # GET should serve the audio bytes
            audio_resp = client.get(
                f"/api/sessions/{session.id}/tts/summary.mp3",
                headers=headers,
            )
            assert audio_resp.status_code == 200
            assert audio_resp.content.startswith(b"FAKE-mp3-")
        finally:
            db.rollback()
            db.close()

    def test_summary_404_other_org(self, client, monkeypatch):
        from services.providers import registry as registry_module

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "tts_c", "test123", "tts_c@test.com", is_superuser=True)
            org_a = _seed_org(db, "A", "tts-a")
            org_b = _seed_org(db, "B", "tts-b")
            _seed_membership(db, user.id, org_a.id, "admin")
            _seed_membership(db, user.id, org_b.id, "admin")
            session_a = _seed_session_with_summary(db, org_a.id, "A Meeting")
            headers_b = _login_headers(client, "tts_c", "test123", "tts-b")

            monkeypatch.setattr(
                registry_module.ProviderRegistry,
                "get_tts",
                lambda self, org_id: _FakeTTSProvider(),
            )

            resp = client.post(
                f"/api/sessions/{session_a.id}/tts/summary",
                headers=headers_b,
            )
            assert resp.status_code == 404
        finally:
            db.rollback()
            db.close()


class TestTTSPodcast:
    """POST /api/sessions/{id}/tts/podcast."""

    def test_podcast_returns_501_for_kokoro(self, client, monkeypatch):
        from services.providers import registry as registry_module

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "tts_d", "test123", "tts_d@test.com", is_superuser=True)
            org = _seed_org(db, "Pod Org", "pod-org")
            _seed_membership(db, user.id, org.id, "admin")
            session = _seed_session_with_summary(db, org.id)
            headers = _login_headers(client, "tts_d", "test123", "pod-org")

            monkeypatch.setattr(
                registry_module.ProviderRegistry,
                "get_tts",
                lambda self, org_id: _FakeKokoroProvider(),
            )

            resp = client.post(
                f"/api/sessions/{session.id}/tts/podcast",
                headers=headers,
            )
            assert resp.status_code == 501, resp.text
            assert "vibevoice" in resp.json()["detail"].lower() or "podcast" in resp.json()["detail"].lower()
        finally:
            db.rollback()
            db.close()

    def test_podcast_synth_with_vibevoice(self, client, monkeypatch):
        from services.providers import registry as registry_module

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "tts_e", "test123", "tts_e@test.com", is_superuser=True)
            org = _seed_org(db, "VV Org", "vv-org")
            _seed_membership(db, user.id, org.id, "admin")
            session = _seed_session_with_summary(db, org.id)
            headers = _login_headers(client, "tts_e", "test123", "vv-org")

            monkeypatch.setattr(
                registry_module.ProviderRegistry,
                "get_tts",
                lambda self, org_id: _FakeTTSProvider(),
            )
            monkeypatch.setattr(
                registry_module.ProviderRegistry,
                "get_llm",
                lambda self, org_id, task="quality": _FakeLLM(),
            )

            resp = client.post(
                f"/api/sessions/{session.id}/tts/podcast?format=mp3",
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["provider"] == "fake"
            assert body["format"] == "mp3"
            assert body["bytes"] > 0
            assert isinstance(body["script"], list) and body["script"], "expected non-empty script"
            sids = {turn["speaker_id"] for turn in body["script"]}
            assert sids.issubset({"host", "analyst"})
            assert body["speakers"]["host"] == "alice"

            # Cached call returns cached=True
            resp2 = client.post(
                f"/api/sessions/{session.id}/tts/podcast?format=mp3",
                headers=headers,
            )
            assert resp2.json()["cached"] is True
        finally:
            db.rollback()
            db.close()


class TestTTSProviderRouting:
    """ProviderRegistry.get_tts() returns the right concrete class per setting."""

    def test_default_returns_kokoro(self, client):
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_tts import KokoroProvider

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            org = _seed_org(db, "Default TTS", "default-tts")
            registry = ProviderRegistry(db)
            provider = registry.get_tts(org.id)
            assert isinstance(provider, KokoroProvider)
            assert provider.name == "kokoro"
            assert provider.supports_podcast is False
        finally:
            db.rollback()
            db.close()

    def test_org_can_switch_to_vibevoice(self, client):
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_tts import VibeVoiceProvider

        _, _, _, SessionLocal, _, OrgProviderSettings = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            org = _seed_org(db, "VV Switch", "vv-switch")
            row = OrgProviderSettings(
                organization_id=org.id,
                service_kind="tts",
                provider_name="vibevoice",
                endpoint_url="http://<infinity-host>:8882",
            )
            db.add(row)
            db.commit()

            registry = ProviderRegistry(db)
            provider = registry.get_tts(org.id)
            assert isinstance(provider, VibeVoiceProvider)
            assert provider.name == "vibevoice"
            assert provider.supports_podcast is True
            assert provider.endpoint == "http://<infinity-host>:8882"
        finally:
            db.rollback()
            db.close()
