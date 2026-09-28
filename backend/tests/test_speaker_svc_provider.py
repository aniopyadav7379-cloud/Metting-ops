"""Tests for ``LocalSpeakerSvcProvider.diarize`` — the canonical production
diarizer (pyannote 3.1 + wespeaker embeddings on meet-speaker-svc).

The reprocess + upload pipelines both call this provider's ``diarize`` to
draw speaker boundaries and pull per-turn voice embeddings. It was the only
shipped diarizer without direct test coverage, so this pins its contract:

  1. A 200 response from speaker-svc /diarize parses each segment's speaker
     + embedding (and stamps the ``backend`` from the payload).
  2. A missing audio path raises before any HTTP call.
  3. A terminal HTTP failure raises instead of masquerading as a successful
     zero-speaker result; pipeline callers can then flag diarization for retry.
  4. A busy GPU (503) is retried and the multipart stream is re-opened.

httpx is mocked with httpx.MockTransport (no network), mirroring
test_sortformer_hybrid.py's approach.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest


def _patch_mock_transport(monkeypatch, handler):
    """Route impl_diarization.httpx.AsyncClient(...) through a MockTransport
    handler instead of the network. Drops the timeout kwarg the provider
    passes (MockTransport ignores it)."""
    import services.providers.impl_diarization as impl

    real_async_client = httpx.AsyncClient

    def _factory(*_args, **_kwargs):
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(impl.httpx, "AsyncClient", _factory)


def _provider():
    from services.providers.impl_diarization import LocalSpeakerSvcProvider

    return LocalSpeakerSvcProvider(endpoint="http://sp:8889")


def test_diarize_parses_segments_speaker_and_embedding(monkeypatch, tmp_path):
    wav = tmp_path / "session.wav"
    wav.write_bytes(b"RIFF....WAVE")  # content irrelevant; transport mocked

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "embedding": [0.1, 0.2]},
                    {"start": 2.5, "end": 4.0, "speaker": "SPEAKER_01", "embedding": [0.3, 0.4]},
                ],
                "num_speakers": 2,
                "backend": "pyannote",
            },
        )

    _patch_mock_transport(monkeypatch, handler)
    segs = asyncio.run(_provider().diarize(str(wav)))

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/diarize")
    # Audio is streamed as a multipart upload (host-topology-proof).
    assert captured["content_type"].startswith("multipart/form-data")
    assert len(segs) == 2
    assert segs[0]["speaker"] == "SPEAKER_00"
    assert segs[0]["embedding"] == [0.1, 0.2]
    assert segs[1]["speaker"] == "SPEAKER_01"
    # backend is stamped onto every segment from the payload.
    assert segs[0]["backend"] == "pyannote"
    assert segs[1]["backend"] == "pyannote"


def test_diarize_missing_file_raises(tmp_path):
    missing = tmp_path / "nope.wav"
    # No transport patch needed — the provider must short-circuit before any
    # HTTP call when the audio path doesn't exist on disk.
    with pytest.raises(FileNotFoundError, match="audio file is missing"):
        asyncio.run(_provider().diarize(str(missing)))


def test_diarize_terminal_http_error_raises(monkeypatch, tmp_path):
    wav = tmp_path / "session.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setenv("SPEAKER_SVC_RETRY_ATTEMPTS", "1")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="speaker-svc exploded")

    _patch_mock_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match=r"HTTP 500"):
        asyncio.run(_provider().diarize(str(wav)))


def test_diarize_retries_busy_gpu_and_ignores_deprecated_threshold(
    monkeypatch, tmp_path
):
    wav = tmp_path / "session.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setenv("SPEAKER_SVC_RETRY_ATTEMPTS", "2")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": "SPEAKER_00",
                        "embedding": [0.1, 0.2],
                    }
                ],
                "backend": "pyannote",
            },
        )

    _patch_mock_transport(monkeypatch, handler)
    segments = asyncio.run(
        _provider().diarize(str(wav), clustering_threshold=0.8)
    )

    assert len(requests) == 2
    assert segments[0]["speaker"] == "SPEAKER_00"
    # The service fixes clustering threshold at startup; sending the stale
    # per-request field makes speaker-svc reject the request with HTTP 400.
    assert all(b"clustering_threshold" not in request.content for request in requests)
