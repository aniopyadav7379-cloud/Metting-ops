"""Unit tests for sortformer-svc — exercise the HTTP surface without
loading the real NeMo Sortformer model.

These tests monkey-patch `_load_model` and `_diarize_wav_array` so we
hit the FastAPI routes, header parsing, and Pydantic response shapes
on CPU in a few hundred milliseconds. End-to-end probe-passes-on-real-
pipeline check happens out-of-process via the spike's manual
verification step (see `docs/phase-b3-sortformer-spike.md`).

Run from the sortformer-svc directory:
    pytest tests/ -v
"""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

# Make `import main` work — pytest pulled in from tests/ dir.
SVC_ROOT = Path(__file__).resolve().parent.parent
if str(SVC_ROOT) not in sys.path:
    sys.path.insert(0, str(SVC_ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ---------- helpers ----------


def _silence_wav_bytes(seconds: float = 1.0, sr: int = 16000) -> bytes:
    """Build a minimal PCM16 WAV in-memory. Just header + zeros — the
    tests don't care about content, only that soundfile can decode it.
    """
    import soundfile as sf
    n = int(seconds * sr)
    buf = io.BytesIO()
    sf.write(buf, np.zeros(n, dtype=np.float32), sr, subtype="PCM_16",
             format="WAV")
    return buf.getvalue()


def _raw_pcm16_bytes(seconds: float = 1.0, sr: int = 16000) -> bytes:
    """Raw little-endian PCM16 at sr — exercises the raw-PCM fallback
    in _decode_audio."""
    n = int(seconds * sr)
    return struct.pack(f"<{n}h", *([0] * n))


def _fake_diarize_result(window_seconds: float = 33.5) -> dict:
    """Plausible 2-speaker result the way Sortformer v1 returns it
    (already parsed into the dict shape _diarize_wav_array yields)."""
    return {
        "speakers": [
            {"spk_id": 0, "spk_label": "speaker_0",
             "start_ms": 0, "end_ms": 2560},
            {"spk_id": 0, "spk_label": "speaker_0",
             "start_ms": 3040, "end_ms": 8720},
            {"spk_id": 1, "spk_label": "speaker_1",
             "start_ms": 15840, "end_ms": 21680},
            {"spk_id": 1, "spk_label": "speaker_1",
             "start_ms": 22080, "end_ms": int(window_seconds * 1000)},
        ],
        "elapsed": 0.42,
        "model": "nvidia/diar_sortformer_4spk-v1",
    }


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Stand up a TestClient with a fake "loaded model" so the routes
    don't 503. We pretend the lifespan fired and a model is resident."""
    monkeypatch.setattr(main, "_model", object())
    monkeypatch.setattr(main, "_model_name",
                        "nvidia/diar_sortformer_4spk-v1")
    monkeypatch.setattr(main, "_model_load_error", None)
    monkeypatch.setattr(main, "_model_load_seconds", 2.5)
    monkeypatch.setattr(main, "_request_count", 0)
    return TestClient(main.app)


# ---------- /health + /readyz ----------


def test_health_ok_when_model_loaded(client):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model_loaded"] is True
    assert data["model"] == "nvidia/diar_sortformer_4spk-v1"
    assert data["requested_model"] == "nvidia/diar_sortformer_4spk-v1"
    assert data["max_speakers"] == 4
    assert data["phase"] == "B.3-spike"
    assert data["version"] == "0.1.0-spike"


def test_readyz_mirrors_health(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model_loaded"] is True


def test_health_degraded_when_load_failed(monkeypatch):
    """If startup load fails the service still comes up so /health can
    report the error to the operator."""
    monkeypatch.setattr(main, "_model", None)
    monkeypatch.setattr(main, "_model_load_error",
                        "RuntimeError: simulated load failure")
    monkeypatch.setattr(main, "_model_name", "")
    c = TestClient(main.app)
    resp = c.get("/health")
    # Still 200 — /health is a status report, not a gate.
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["model_loaded"] is False
    assert "simulated load failure" in data["load_error"]


def test_health_loading_when_in_progress(monkeypatch):
    monkeypatch.setattr(main, "_model", None)
    monkeypatch.setattr(main, "_model_load_error", None)
    monkeypatch.setattr(main, "_model_name", "")
    c = TestClient(main.app)
    resp = c.get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "loading"


# ---------- /diarize-stream happy path ----------


def test_diarize_stream_wav_two_speakers(client, monkeypatch):
    monkeypatch.setattr(
        main, "_diarize_wav_array",
        lambda wav: _fake_diarize_result(33.5),
    )

    resp = client.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=33.5),
        headers={
            "Content-Type": "audio/wav",
            "X-Session-Id": "session-abc",
            "X-Flush-Sequence": "7",
            "X-Is-Final": "0",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "nvidia/diar_sortformer_4spk-v1"
    assert data["distinct_speaker_count"] == 2
    assert data["sequence"] == 7
    assert data["is_final"] is False
    assert len(data["speakers"]) == 4
    # Sorted by start_ms.
    starts = [s["start_ms"] for s in data["speakers"]]
    assert starts == sorted(starts)
    # First two segments are speaker 0, last two are speaker 1.
    assert data["speakers"][0]["spk_id"] == 0
    assert data["speakers"][-1]["spk_id"] == 1


def test_diarize_stream_raw_pcm_fallback(client, monkeypatch):
    monkeypatch.setattr(
        main, "_diarize_wav_array",
        lambda wav: _fake_diarize_result(1.0),
    )
    resp = client.post(
        "/diarize-stream",
        content=_raw_pcm16_bytes(seconds=1.0),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Sanity: the decode path ran and returned a result envelope.
    assert "speakers" in data
    assert data["distinct_speaker_count"] == 2


def test_diarize_stream_window_start_ms_offsets_response(client, monkeypatch):
    monkeypatch.setattr(
        main, "_diarize_wav_array",
        lambda wav: _fake_diarize_result(33.5),
    )
    OFFSET = 60_000  # caller-reported absolute timeline
    resp = client.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=33.5),
        headers={
            "Content-Type": "audio/wav",
            "X-Window-Start-Ms": str(OFFSET),
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # All start_ms should be shifted by OFFSET.
    assert data["speakers"][0]["start_ms"] == 0 + OFFSET
    assert data["speakers"][0]["end_ms"] == 2560 + OFFSET
    assert data["window_end_ms"] == OFFSET + int(round(33.5 * 1000))


def test_diarize_stream_is_final_echoed(client, monkeypatch):
    monkeypatch.setattr(
        main, "_diarize_wav_array",
        lambda wav: _fake_diarize_result(1.0),
    )
    resp = client.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=1.0),
        headers={"X-Is-Final": "1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_final"] is True


# ---------- /diarize-stream guards ----------


def test_diarize_stream_empty_body_400(client):
    resp = client.post("/diarize-stream", content=b"")
    assert resp.status_code == 400, resp.text
    assert "empty" in resp.json()["detail"].lower()


def test_diarize_stream_too_short_returns_empty(client, monkeypatch):
    """Sub-MIN_AUDIO_SECONDS buffers return an empty response (200)
    rather than 400 so the WS handler's de-dup logic stays simple."""
    # Bypass the model path entirely with a fake that errors if called.
    monkeypatch.setattr(
        main, "_diarize_wav_array",
        lambda wav: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    resp = client.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=0.1),  # below 0.5s default
        headers={"Content-Type": "audio/wav"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["speakers"] == []
    assert data["distinct_speaker_count"] == 0


def test_diarize_stream_too_long_413(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_STREAM_AUDIO_SECONDS", 5.0)
    resp = client.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=10.0),
        headers={"Content-Type": "audio/wav"},
    )
    assert resp.status_code == 413, resp.text
    assert "too long" in resp.json()["detail"]


def test_diarize_stream_503_when_load_failed(monkeypatch):
    """If load failed, /diarize-stream returns 503 with the error."""
    monkeypatch.setattr(main, "_model", None)
    monkeypatch.setattr(main, "_model_load_error",
                        "RuntimeError: HF download failed")
    c = TestClient(main.app)
    resp = c.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=1.0),
        headers={"Content-Type": "audio/wav"},
    )
    assert resp.status_code == 503, resp.text
    assert "HF download failed" in resp.json()["detail"]


def test_diarize_stream_503_when_still_loading(monkeypatch):
    monkeypatch.setattr(main, "_model", None)
    monkeypatch.setattr(main, "_model_load_error", None)
    c = TestClient(main.app)
    resp = c.post(
        "/diarize-stream",
        content=_silence_wav_bytes(seconds=1.0),
        headers={"Content-Type": "audio/wav"},
    )
    assert resp.status_code == 503, resp.text
    assert "still loading" in resp.json()["detail"]


# ---------- /diarize-file ----------


def test_diarize_file_path_missing_400(client):
    resp = client.post("/diarize-file?audio_path=/no/such/file.wav")
    assert resp.status_code == 400, resp.text
    assert "not found" in resp.json()["detail"]


def test_diarize_file_path_too_short_400(client, tmp_path):
    """A real on-disk file that's < MIN_AUDIO_SECONDS triggers the
    duration guard."""
    import soundfile as sf
    p = tmp_path / "tiny.wav"
    sf.write(str(p), np.zeros(80, dtype=np.float32), 16000, subtype="PCM_16")
    resp = client.post(f"/diarize-file?audio_path={p}")
    assert resp.status_code == 400, resp.text
    assert "too short" in resp.json()["detail"]


def test_diarize_file_ok(client, monkeypatch, tmp_path):
    """File that's long enough + non-empty fake-result -> 200 with
    correct shape."""
    import soundfile as sf
    p = tmp_path / "ok.wav"
    sf.write(str(p), np.zeros(16000 * 5, dtype=np.float32), 16000,
             subtype="PCM_16")
    monkeypatch.setattr(
        main, "_diarize_path",
        lambda path: _fake_diarize_result(5.0),
    )
    resp = client.post(f"/diarize-file?audio_path={p}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "nvidia/diar_sortformer_4spk-v1"
    assert data["distinct_speaker_count"] == 2
    assert data["is_final"] is True


# ---------- /diarize-file-upload (v2.2.0 canonical hybrid) ----------


def test_diarize_file_upload_pyannote_shape(client, monkeypatch):
    """Happy path: multipart WAV in, pyannote-compatible DiarizeResponse
    out. Embeddings are stubbed so we don't touch speaker-svc."""
    monkeypatch.setattr(
        main, "_diarize_wav_array", lambda wav: _fake_diarize_result(33.5)
    )

    async def _fake_embed(wav, turns):
        # Embed every turn except the last (exercises the None passthrough).
        return [[0.1, 0.2, 0.3] if i < len(turns) - 1 else None
                for i in range(len(turns))]

    monkeypatch.setattr(main, "_embed_turns_via_speaker_svc", _fake_embed)

    resp = client.post(
        "/diarize-file-upload",
        files={"audio": ("session.wav", _silence_wav_bytes(33.5), "audio/wav")},
        data={"return_embeddings": "true"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["backend"] == "sortformer-hybrid"
    assert data["num_speakers"] == 2
    assert data["model"] == "nvidia/diar_sortformer_4spk-v1"
    assert len(data["segments"]) == 4
    # spk_id -> SPEAKER_NN, seconds-based start/end.
    assert data["segments"][0]["speaker"] == "SPEAKER_00"
    assert data["segments"][-1]["speaker"] == "SPEAKER_01"
    assert data["segments"][0]["start"] == 0.0
    assert data["segments"][0]["end"] == pytest.approx(2.56)
    assert data["segments"][0]["embedding"] == [0.1, 0.2, 0.3]
    # Last turn embedding came back None.
    assert data["segments"][-1]["embedding"] is None


def test_diarize_file_upload_skips_embeddings_when_disabled(client, monkeypatch):
    monkeypatch.setattr(
        main, "_diarize_wav_array", lambda wav: _fake_diarize_result(5.0)
    )

    called = {"n": 0}

    async def _fake_embed(wav, turns):  # pragma: no cover - must not run
        called["n"] += 1
        return [None] * len(turns)

    monkeypatch.setattr(main, "_embed_turns_via_speaker_svc", _fake_embed)

    resp = client.post(
        "/diarize-file-upload",
        files={"audio": ("session.wav", _silence_wav_bytes(5.0), "audio/wav")},
        data={"return_embeddings": "false"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["embeddings_enabled"] is False
    assert called["n"] == 0  # embed helper never invoked
    assert all(s["embedding"] is None for s in data["segments"])


def test_diarize_file_upload_empty_body_400(client):
    resp = client.post(
        "/diarize-file-upload",
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    assert resp.status_code == 400, resp.text


def test_diarize_file_upload_503_when_model_down(monkeypatch):
    monkeypatch.setattr(main, "_model", None)
    monkeypatch.setattr(
        main, "_model_load_error", "RuntimeError: HF download failed"
    )
    c = TestClient(main.app)
    resp = c.post(
        "/diarize-file-upload",
        files={"audio": ("session.wav", _silence_wav_bytes(5.0), "audio/wav")},
    )
    assert resp.status_code == 503, resp.text
    assert "HF download failed" in resp.json()["detail"]


def test_embed_turns_via_speaker_svc_crops_and_skips_short(monkeypatch):
    """_embed_turns_via_speaker_svc embeds turns >= EMBED_MIN_SECONDS and
    leaves shorter turns as None, calling speaker-svc once per long turn."""
    import asyncio

    import httpx

    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        posts["n"] += 1
        return httpx.Response(200, json={"embedding": [0.5, 0.6], "embedding_dim": 2})

    real_async_client = httpx.AsyncClient

    def _factory(*_a, **_k):
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main.httpx, "AsyncClient", _factory)

    wav = np.zeros(16000 * 4, dtype=np.float32)  # 4 s of audio
    turns = [
        {"spk_id": 0, "start_ms": 0, "end_ms": 2000},      # 2.0 s -> embed
        {"spk_id": 1, "start_ms": 2000, "end_ms": 2200},   # 0.2 s -> skip
        {"spk_id": 0, "start_ms": 2200, "end_ms": 4000},   # 1.8 s -> embed
    ]
    out = asyncio.run(main._embed_turns_via_speaker_svc(wav, turns))
    assert out[0] == [0.5, 0.6]
    assert out[1] is None  # too short, never sent
    assert out[2] == [0.5, 0.6]
    assert posts["n"] == 2  # only the two long turns hit speaker-svc


def test_embed_turns_via_speaker_svc_survives_unreachable_svc(monkeypatch):
    """If speaker-svc refuses the connection, embeddings come back all-None
    and the helper stops early instead of raising."""
    import asyncio

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    real_async_client = httpx.AsyncClient

    def _factory(*_a, **_k):
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main.httpx, "AsyncClient", _factory)

    wav = np.zeros(16000 * 4, dtype=np.float32)
    turns = [
        {"spk_id": 0, "start_ms": 0, "end_ms": 2000},
        {"spk_id": 1, "start_ms": 2000, "end_ms": 4000},
    ]
    out = asyncio.run(main._embed_turns_via_speaker_svc(wav, turns))
    assert out == [None, None]


# ---------- segment string parser ----------


def test_parse_segment_str_canonical():
    out = main._parse_segment_str("0.000 2.560 speaker_0")
    assert out == {
        "spk_id": 0, "spk_label": "speaker_0",
        "start_ms": 0, "end_ms": 2560,
    }


def test_parse_segment_str_alt_label():
    out = main._parse_segment_str("3.0 5.5 spk_2")
    assert out["spk_id"] == 2
    assert out["start_ms"] == 3000
    assert out["end_ms"] == 5500


def test_parse_segment_str_garbage_returns_none():
    assert main._parse_segment_str("garbage") is None
    assert main._parse_segment_str("0.0") is None
    assert main._parse_segment_str("a b c") is None


def test_parse_segment_str_unknown_label_hashes_into_range():
    """An unparseable speaker label still yields a valid spk_id in
    [0, MAX_SPEAKERS) via hash-mod."""
    out = main._parse_segment_str("0.0 1.0 alice")
    assert 0 <= out["spk_id"] < main.MAX_SPEAKERS
    assert out["spk_label"] == "alice"
