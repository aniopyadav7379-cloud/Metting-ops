"""v2.2.0 sortformer canonical-hybrid diarization tests.

Covers the three pieces the hybrid path adds:

  1. registry.get_diarization provider selection — defaults to the
     pyannote LocalSpeakerSvcProvider, switches to
     SortformerSpeakerSvcProvider when SPEAKER_PROVIDER_PREFERENCE (or a
     per-org provider_name override) asks for the hybrid.
  2. SortformerSpeakerSvcProvider.diarize — posts the WAV to
     sortformer-svc /diarize-file-upload and parses the pyannote-shaped
     response into the same list[dict] LocalSpeakerSvcProvider returns
     (start/end/speaker/embedding/backend), with graceful empties on
     missing-file / HTTP error.
  3. embed / embed_bytes / identify delegate to the wrapped wespeaker
     provider — the hybrid only changes who draws speaker boundaries,
     not who owns speaker identity.

httpx is mocked with httpx.MockTransport (no network), matching
test_brigade_writer's style. Async provider methods are driven with
asyncio.run(), matching test_summary_slices / test_tier.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest


# ---------------------------------------------------------------------------
# registry provider selection
# ---------------------------------------------------------------------------


def _make_registry(settings):
    """ProviderRegistry with the diarization settings cache pre-primed so
    get_diarization never touches the DB. `settings` is the dict
    _get_settings would return (or None for "no org settings row")."""
    from services.providers.registry import ProviderRegistry

    reg = ProviderRegistry(db=None)
    reg._settings_cache[(1, "diarization")] = settings
    return reg


def test_registry_defaults_to_pyannote(monkeypatch):
    monkeypatch.delenv("SPEAKER_PROVIDER_PREFERENCE", raising=False)
    from services.providers.impl_diarization import LocalSpeakerSvcProvider

    prov = _make_registry(None).get_diarization(1)
    assert isinstance(prov, LocalSpeakerSvcProvider)
    # Plain pyannote provider, not the hybrid subclass.
    assert type(prov).__name__ == "LocalSpeakerSvcProvider"


def test_registry_env_selects_sortformer_hybrid(monkeypatch):
    monkeypatch.setenv("SPEAKER_PROVIDER_PREFERENCE", "sortformer-hybrid")
    monkeypatch.setenv("SORTFORMER_URL", "http://sf:8896")
    monkeypatch.setenv("SPEAKER_SVC_URL", "http://sp:8889")
    from services.providers.impl_diarization import (
        LocalSpeakerSvcProvider,
        SortformerSpeakerSvcProvider,
    )

    prov = _make_registry(None).get_diarization(1)
    assert isinstance(prov, SortformerSpeakerSvcProvider)
    assert prov.endpoint == "http://sf:8896"
    # Embeddings still flow through wespeaker at the speaker-svc endpoint.
    assert isinstance(prov._speaker_svc, LocalSpeakerSvcProvider)
    assert prov._speaker_svc.endpoint == "http://sp:8889"


def test_registry_per_org_override_beats_env(monkeypatch):
    """Env says pyannote, org row says sortformer-hybrid → hybrid wins, and
    the org's endpoint_url is used as the (wespeaker) speaker-svc address."""
    monkeypatch.setenv("SPEAKER_PROVIDER_PREFERENCE", "pyannote")
    monkeypatch.setenv("SORTFORMER_URL", "http://sf:8896")
    from services.providers.impl_diarization import SortformerSpeakerSvcProvider

    reg = _make_registry(
        {"provider_name": "sortformer-hybrid", "endpoint_url": "http://sp-custom:8889"}
    )
    prov = reg.get_diarization(1)
    assert isinstance(prov, SortformerSpeakerSvcProvider)
    assert prov._speaker_svc.endpoint == "http://sp-custom:8889"


# ---------------------------------------------------------------------------
# SortformerSpeakerSvcProvider.diarize
# ---------------------------------------------------------------------------


def _patch_mock_transport(monkeypatch, handler):
    """Make impl_diarization.httpx.AsyncClient(...) route through a
    MockTransport handler instead of the network. Drops the timeout kwarg
    the provider passes (MockTransport ignores it)."""
    import services.providers.impl_diarization as impl

    real_async_client = httpx.AsyncClient

    def _factory(*_args, **_kwargs):
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(impl.httpx, "AsyncClient", _factory)


def _provider():
    from services.providers.impl_diarization import SortformerSpeakerSvcProvider

    return SortformerSpeakerSvcProvider(
        endpoint="http://sf:8896", speaker_svc_endpoint="http://sp:8889"
    )


def test_diarize_parses_pyannote_shaped_response(monkeypatch, tmp_path):
    wav = tmp_path / "session.wav"
    wav.write_bytes(b"RIFF....WAVE")  # content irrelevant; transport mocked

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "embedding": [0.1, 0.2]},
                    {"start": 2.5, "end": 4.0, "speaker": "SPEAKER_01", "embedding": None},
                ],
                "num_speakers": 2,
                "backend": "sortformer-hybrid",
                "duration_seconds": 4.0,
                "model": "nvidia/diar_sortformer_4spk-v1",
                "rtf": 0.03,
            },
        )

    _patch_mock_transport(monkeypatch, handler)
    segs = asyncio.run(_provider().diarize(str(wav)))

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/diarize-file-upload")
    assert len(segs) == 2
    assert segs[0]["speaker"] == "SPEAKER_00"
    assert segs[0]["embedding"] == [0.1, 0.2]
    # backend is stamped onto every segment so stored transcripts show
    # which diarizer produced them.
    assert segs[0]["backend"] == "sortformer-hybrid"
    assert segs[1]["embedding"] is None


def test_diarize_missing_file_returns_empty(tmp_path):
    missing = tmp_path / "nope.wav"
    assert asyncio.run(_provider().diarize(str(missing))) == []


def test_diarize_http_error_returns_empty(monkeypatch, tmp_path):
    wav = tmp_path / "session.wav"
    wav.write_bytes(b"RIFF")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="sortformer exploded")

    _patch_mock_transport(monkeypatch, handler)
    assert asyncio.run(_provider().diarize(str(wav))) == []


def test_diarize_ignores_speaker_count_hints(monkeypatch, tmp_path):
    """num_speakers / clustering hints are accepted for protocol parity but
    must not be forwarded — Sortformer has no such knobs. We assert the
    call still succeeds and the multipart body is what we expect."""
    wav = tmp_path / "session.wav"
    wav.write_bytes(b"RIFF")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(
            200,
            json={
                "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "embedding": None}],
                "num_speakers": 1,
                "backend": "sortformer-hybrid",
                "duration_seconds": 1.0,
                "model": "nvidia/diar_sortformer_4spk-v1",
                "rtf": 0.02,
            },
        )

    _patch_mock_transport(monkeypatch, handler)
    segs = asyncio.run(
        _provider().diarize(str(wav), num_speakers=5, clustering_threshold=0.8)
    )
    assert len(segs) == 1
    assert seen["content_type"].startswith("multipart/form-data")


# ---------------------------------------------------------------------------
# embed / identify delegate to wespeaker
# ---------------------------------------------------------------------------


class _FakeWespeaker:
    def __init__(self):
        self.calls: list[tuple] = []

    async def embed(self, audio_path):
        self.calls.append(("embed", audio_path))
        return {"embedding": [1.0], "embedding_dim": 1}

    async def embed_bytes(self, audio_bytes, filename="clip.wav"):
        self.calls.append(("embed_bytes", filename))
        return {"embedding": [2.0], "embedding_dim": 1}

    async def identify(self, embedding, candidates, threshold=0.55):
        self.calls.append(("identify", threshold))
        return {"matches": [], "best_match": None}

    async def health(self):
        return {"status": "ok", "kind": "wespeaker"}


def test_embed_identify_delegate_to_wespeaker():
    prov = _provider()
    fake = _FakeWespeaker()
    prov._speaker_svc = fake

    assert asyncio.run(prov.embed("/clip.wav"))["embedding"] == [1.0]
    assert asyncio.run(prov.embed_bytes(b"x"))["embedding"] == [2.0]
    assert "best_match" in asyncio.run(prov.identify([1.0], []))

    kinds = [c[0] for c in fake.calls]
    assert kinds == ["embed", "embed_bytes", "identify"]


def test_health_includes_wespeaker_dependency(monkeypatch):
    """provider.health() reports sortformer-svc health plus the wespeaker
    svc it depends on for embeddings."""
    prov = _provider()
    prov._speaker_svc = _FakeWespeaker()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "model_loaded": True})

    _patch_mock_transport(monkeypatch, handler)
    out = asyncio.run(prov.health())
    assert out["status"] == "ok"
    assert out["speaker_svc"]["kind"] == "wespeaker"
