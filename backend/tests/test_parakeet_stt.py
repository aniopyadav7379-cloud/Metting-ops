"""Tests for Phase 4c-1: Parakeet STT alongside whisper.cpp.

These cover the bits we OWN:
  * `available_providers` list contains both `local_whisper` and `parakeet`.
  * `ProviderRegistry.get_stt(org_id)` resolves to the right provider class
    based on the per-org setting, and falls through to whisper by default.
  * The dummy whisper / parakeet providers expose the same `transcribe`
    signature so the upload pipeline can call either without branching.

We intentionally do NOT exercise the live HTTP endpoints, those are covered
by the curl smoke tests in deploy/bigboy/STATUS.md.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import OrgProviderSettings
    return Organization, User, UserOrganization, SessionLocal, OrgProviderSettings


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
    _, User, _, _, _ = _models()
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
    Organization, _, _, _, _ = _models()
    org = Organization(name=name, slug=slug, is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _seed_membership(db, user_id: int, org_id: int, role: str = "user"):
    _, _, UserOrganization, _, _ = _models()
    mem = UserOrganization(user_id=user_id, organization_id=org_id, role=role)
    db.add(mem)
    db.commit()
    return mem


class TestSTTAvailableProviders:
    """`/api/providers/available/stt` should advertise parakeet."""

    def test_available_stt_includes_parakeet(self, client):
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "stt_avail", "test123", "stt_avail@test.com")
            org = _seed_org(db, "STT Avail Org", "stt-avail-org")
            _seed_membership(db, user.id, org.id, "user")
            headers = _login_headers(client, "stt_avail", "test123", "stt-avail-org")

            resp = client.get("/api/providers/available/stt", headers=headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["service_kind"] == "stt"
            providers = body["providers"]
            assert "local_whisper" in providers
            assert "parakeet" in providers
        finally:
            db.rollback()
            db.close()


class TestProviderRegistrySTTRouting:
    """Registry resolves the right STT provider per org-level setting.

    Takes the session-scoped `app` fixture so table creation runs first.
    Without it these tests fail in isolation with "no such table" because
    `app` is the fixture that calls Base.metadata.create_all(...).
    """

    def test_default_is_local_whisper(self, app, monkeypatch):
        """With STT_DEFAULT_PROVIDER=local_whisper, registry falls back to whisper
        when no per-org row exists. (Codebase default flipped to parakeet on
        the Phase B.2 commit, so this test now explicitly pins the legacy
        whisper default.)
        """
        monkeypatch.setenv("STT_DEFAULT_PROVIDER", "local_whisper")
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_stt import LocalWhisperProvider
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            registry = ProviderRegistry(db)
            stt = registry.get_stt(org_id=999_999)  # any org with no settings row
            assert isinstance(stt, LocalWhisperProvider)
        finally:
            db.rollback()
            db.close()

    def test_default_is_parakeet_in_production(self, app, monkeypatch):
        """No STT_DEFAULT_PROVIDER env -> default is parakeet (current prod default)."""
        monkeypatch.delenv("STT_DEFAULT_PROVIDER", raising=False)
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_stt import LocalParakeetProvider
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            registry = ProviderRegistry(db)
            stt = registry.get_stt(org_id=999_998)
            assert isinstance(stt, LocalParakeetProvider)
        finally:
            db.rollback()
            db.close()

    def test_parakeet_resolves_when_provider_name_set(self, app, monkeypatch):
        """When provider_name=parakeet is set, registry returns Parakeet provider
        and falls back to the in-cluster URL when PARAKEET_SERVER_URL isn't set.

        Production sets PARAKEET_SERVER_URL=http://meet-parakeet-svc:8881, so we
        clear it here to assert the in-cluster default path.
        """
        monkeypatch.delenv("PARAKEET_SERVER_URL", raising=False)

        from services.providers.registry import ProviderRegistry
        from services.providers.impl_stt import LocalParakeetProvider
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()

            org = _seed_org(db, "Parakeet Org", "parakeet-org")
            row = OrgProviderSettings(
                organization_id=org.id,
                service_kind="stt",
                provider_name="parakeet",
            )
            db.add(row)
            db.commit()

            registry = ProviderRegistry(db)
            stt = registry.get_stt(org_id=org.id)
            assert isinstance(stt, LocalParakeetProvider)
            assert stt.endpoint.startswith("http://meet-parakeet-svc:")
        finally:
            db.rollback()
            db.close()

    def test_gateway_resolves_when_provider_name_set(self, app, monkeypatch):
        """provider_name in {openai,gateway,litellm} -> OpenAITranscriptionProvider,
        endpoint from OPENAI_API_BASE, model defaults to 'parakeet'."""
        monkeypatch.setenv("OPENAI_API_BASE", "http://gw.example:4000/v1")
        monkeypatch.delenv("STT_OPENAI_URL", raising=False)
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_stt import OpenAITranscriptionProvider
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            org = _seed_org(db, "Gateway STT Org", "gateway-stt-org")
            db.add(OrgProviderSettings(
                organization_id=org.id, service_kind="stt", provider_name="gateway",
            ))
            db.commit()
            stt = ProviderRegistry(db).get_stt(org_id=org.id)
            assert isinstance(stt, OpenAITranscriptionProvider)
            assert stt.endpoint == "http://gw.example:4000/v1"
            assert stt.model == "parakeet"
        finally:
            db.rollback()
            db.close()

    def test_gateway_kinds_get_default_key(self, app, monkeypatch):
        """get_api_key returns the default (gateway) key for every gateway-routed
        kind — not just llm — so gateway STT/TTS authenticate. Non-gateway kinds
        (e.g. diarization) still get no default."""
        from services.providers import registry as reg_mod
        monkeypatch.setattr(reg_mod, "DEFAULT_LITELLM_API_KEY", "sk-gw-default")
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            r = reg_mod.ProviderRegistry(db)
            assert r.get_api_key(424242, "stt") == "sk-gw-default"
            assert r.get_api_key(424242, "tts") == "sk-gw-default"
            assert r.get_api_key(424242, "llm") == "sk-gw-default"
            assert r.get_api_key(424242, "diarization") == ""
        finally:
            db.rollback()
            db.close()

    def test_unknown_provider_falls_back_to_whisper(self, app):
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_stt import LocalWhisperProvider
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()

            org = _seed_org(db, "Unknown STT Org", "unknown-stt-org")
            row = OrgProviderSettings(
                organization_id=org.id,
                service_kind="stt",
                provider_name="some-future-stt",
            )
            db.add(row)
            db.commit()

            registry = ProviderRegistry(db)
            stt = registry.get_stt(org_id=org.id)
            assert isinstance(stt, LocalWhisperProvider)
        finally:
            db.rollback()
            db.close()


class TestProviderRegistrySTTParakeetUpdate:
    """End-to-end: org admin sets STT to parakeet via the API, registry honors it."""

    def test_admin_can_set_stt_to_parakeet(self, client):
        from services.providers.registry import ProviderRegistry
        from services.providers.impl_stt import LocalParakeetProvider
        _, _, _, SessionLocal, OrgProviderSettings = _models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()
            user = _seed_user(db, "stt_admin", "test123", "stt_admin@test.com")
            org = _seed_org(db, "STT Admin Org", "stt-admin-org")
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, "stt_admin", "test123", "stt-admin-org")

            resp = client.put(
                "/api/providers/stt",
                json={"provider_name": "parakeet", "model_name": "parakeet-tdt-1.1b"},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["provider_name"] == "parakeet"
            assert data["model_name"] == "parakeet-tdt-1.1b"

            # Now the registry should resolve to the Parakeet provider.
            registry = ProviderRegistry(db)
            stt = registry.get_stt(org_id=org.id)
            assert isinstance(stt, LocalParakeetProvider)
            assert stt.model == "parakeet-tdt-1.1b"
        finally:
            db.rollback()
            db.close()


class TestSTTProviderProtocol:
    """Both providers must expose the same async transcribe(audio_path, language=...) shape."""

    def test_whisper_and_parakeet_share_signature(self):
        from services.providers.impl_stt import LocalParakeetProvider, LocalWhisperProvider

        whisper = LocalWhisperProvider(endpoint="http://meet-whisper:8178")
        parakeet = LocalParakeetProvider(endpoint="http://meet-parakeet-svc:8881")

        for provider in (whisper, parakeet):
            assert hasattr(provider, "transcribe")
            sig = inspect.signature(provider.transcribe)
            assert "audio_path" in sig.parameters
            assert "language" in sig.parameters
            assert inspect.iscoroutinefunction(provider.transcribe)

    def test_missing_file_returns_empty_result_shape(self):
        from services.providers.impl_stt import LocalParakeetProvider, LocalWhisperProvider

        async def _exercise():
            whisper = LocalWhisperProvider(endpoint="http://meet-whisper:8178")
            parakeet = LocalParakeetProvider(endpoint="http://meet-parakeet-svc:8881")
            results = await asyncio.gather(
                whisper.transcribe("/tmp/_does_not_exist_meeting_ops.wav"),
                parakeet.transcribe("/tmp/_does_not_exist_meeting_ops.wav"),
            )
            for result in results:
                assert isinstance(result, dict)
                # Every required key is there even on failure paths.
                for key in ("text", "segments", "words", "duration", "language", "model"):
                    assert key in result, f"missing key {key} in {result}"
                assert result["text"] == ""
                assert result["segments"] == []

        asyncio.run(_exercise())


class TestOpenAIGatewaySTTProvider:
    """The gateway STT provider is a drop-in: same signature, same normalized
    return shape, and it parses the OpenAI verbose_json the gateway returns."""

    def test_shares_signature_and_empty_shape(self, tmp_path):
        from services.providers.impl_stt import (
            LocalParakeetProvider,
            OpenAITranscriptionProvider,
        )

        gw = OpenAITranscriptionProvider(endpoint="http://gw:4000/v1", model="parakeet")
        sig = inspect.signature(gw.transcribe)
        assert "audio_path" in sig.parameters and "language" in sig.parameters
        assert inspect.iscoroutinefunction(gw.transcribe)

        result = asyncio.run(gw.transcribe("/tmp/_nope_meeting_ops.wav"))
        for key in ("text", "segments", "words", "duration", "language", "model"):
            assert key in result
        assert result["text"] == "" and result["segments"] == []

    def test_parses_openai_verbose_json(self, tmp_path, monkeypatch):
        """Feed the exact shape the live gateway returned (segments without a
        per-segment confidence; OpenAI extra fields) and assert normalization."""
        from services.providers import impl_stt
        from services.providers.impl_stt import OpenAITranscriptionProvider

        audio = tmp_path / "clip.wav"
        audio.write_bytes(b"RIFFxxxxWAVE")  # provider only checks existence + opens it

        canned = {
            "text": "welcome everyone to today's test meeting",
            "language": "en",
            "task": "transcribe",
            "duration": 33.71,
            "segments": [{
                "id": 0, "seek": 0, "start": 0.16, "end": 33.52,
                "text": "welcome everyone to today's test meeting",
                "avg_logprob": 0.0, "no_speech_prob": 0.0, "temperature": 0.0,
            }],
            "words": [{"word": "welcome", "start": 0.16, "end": 0.5}],
        }

        class _Resp:
            status_code = 200
            text = ""
            def json(self):
                return canned

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(impl_stt.httpx, "AsyncClient", _Client)

        prov = OpenAITranscriptionProvider(
            endpoint="http://gw:4000/v1", model="parakeet", api_key="k"
        )
        result = asyncio.run(prov.transcribe(str(audio)))
        assert result["text"] == "welcome everyone to today's test meeting"
        assert len(result["segments"]) == 1
        seg = result["segments"][0]
        assert seg["start"] == 0.16 and seg["end"] == 33.52
        assert seg["text"] == "welcome everyone to today's test meeting"
        assert seg["confidence"] == 0.97  # default when the gateway omits it
        assert result["language"] == "en"
        assert result["duration"] == pytest.approx(33.71)
        assert result["model"] == "parakeet"
        assert result["words"][0]["word"] == "welcome"
