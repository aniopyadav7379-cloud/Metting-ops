"""Unit tests for /healthz/synthetic — the full-pipeline diarization probe.

These tests do NOT touch the real pyannote/wespeaker models. They monkey-patch
`_run_diarization_on_wav` (the shared core path /diarize and /healthz/synthetic
both call) so we exercise the probe's branching logic without needing the GPU
or HuggingFace token.

The end-to-end probe-passes-on-real-pipeline check happens out-of-process via
the Docker healthcheck once the container is deployed (see scripts/leak_bench.py
for a similar pattern).

Run from the speaker-svc directory:
    pytest tests/ -v

Run a single test:
    pytest tests/test_healthz_synthetic.py::test_healthz_synthetic_ok -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

# Make `import main` work — pytest pulled in from tests/ dir.
SVC_ROOT = Path(__file__).resolve().parent.parent
if str(SVC_ROOT) not in sys.path:
    sys.path.insert(0, str(SVC_ROOT))

import main  # noqa: E402
from main import DiarizeResponse, DiarizeSegment, app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_meta_cache():
    """Reset the manifest cache between tests so each test sees a fresh load."""
    if hasattr(main, "_synthetic_meta_cache"):
        delattr(main, "_synthetic_meta_cache")
    yield
    if hasattr(main, "_synthetic_meta_cache"):
        delattr(main, "_synthetic_meta_cache")


def _embed_vec(seed: int) -> list[float]:
    """Deterministic unit-norm 256-d embedding for tests."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(256).astype("float32")
    v /= np.linalg.norm(v)
    return v.tolist()


def _two_speaker_result() -> DiarizeResponse:
    """Build a plausible 2-speaker diarization result the probe should accept."""
    return DiarizeResponse(
        segments=[
            DiarizeSegment(start=0.0, end=15.0, speaker="SPEAKER_00", embedding=_embed_vec(1)),
            DiarizeSegment(start=15.5, end=24.0, speaker="SPEAKER_01", embedding=_embed_vec(99)),
            DiarizeSegment(start=24.1, end=33.5, speaker="SPEAKER_01", embedding=_embed_vec(100)),
        ],
        num_speakers=2,
        backend="pyannote-community-1",
        duration_seconds=33.5,
    )


def _one_speaker_result() -> DiarizeResponse:
    """Collapsed-cluster degenerate result — what the bug a few days ago produced."""
    return DiarizeResponse(
        segments=[
            DiarizeSegment(start=0.0, end=33.5, speaker="SPEAKER_00", embedding=_embed_vec(7)),
        ],
        num_speakers=1,
        backend="pyannote-community-1",
        duration_seconds=33.5,
    )


def _two_speaker_too_close_embeddings() -> DiarizeResponse:
    """Speaker count correct but centroids near-identical — wespeaker degraded."""
    base = _embed_vec(42)
    # Make speaker B almost the same vector as A (cosine sim ~1.0, distance ~0.0).
    near_dup = np.asarray(base, dtype="float32")
    # Tiny perturbation in just one dim to keep norms healthy.
    near_dup[0] += 0.001
    near_dup = near_dup / np.linalg.norm(near_dup)
    return DiarizeResponse(
        segments=[
            DiarizeSegment(start=0.0, end=15.0, speaker="SPEAKER_00", embedding=base),
            DiarizeSegment(start=15.5, end=33.5, speaker="SPEAKER_01", embedding=near_dup.tolist()),
        ],
        num_speakers=2,
        backend="pyannote-community-1",
        duration_seconds=33.5,
    )


def test_fixture_files_present():
    """The bundled fixture + manifest must ship inside the service directory."""
    assert main.SYNTHETIC_FIXTURE_PATH.exists(), (
        f"missing fixture wav at {main.SYNTHETIC_FIXTURE_PATH}"
    )
    assert main.SYNTHETIC_META_PATH.exists(), (
        f"missing fixture manifest at {main.SYNTHETIC_META_PATH}"
    )
    meta = json.loads(main.SYNTHETIC_META_PATH.read_text())
    assert meta["expected_speaker_count"] == 2
    assert meta["filename"] == "synthetic_2speaker.wav"
    assert "expected_min_embedding_distance" in meta


def test_healthz_synthetic_ok(client, monkeypatch):
    """Pipeline returns 2 speakers + sane segments + distinct embeddings -> 200 OK."""

    def fake_run(wav, **kwargs):
        # The fixture should have been read before this is called.
        assert isinstance(wav, np.ndarray)
        assert wav.ndim == 1
        assert kwargs.get("return_embeddings") is True
        return _two_speaker_result()

    monkeypatch.setattr(main, "_run_diarization_on_wav", fake_run)

    resp = client.get("/healthz/synthetic")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["speaker_count"] == 2
    assert data["segments"] == 3
    assert data["elapsed_ms"] >= 0
    assert data["fixture"] == "synthetic_2speaker.wav"
    assert data["backend"] == "pyannote-community-1"
    assert data["checked_at"].endswith("Z")
    # Random unit vectors have high cosine distance, well above the 0.4 floor.
    assert "embedding_distance" in data
    assert data["embedding_distance"] >= data["embedding_distance_required"]


def test_healthz_synthetic_speaker_count_mismatch(client, monkeypatch):
    """Pipeline collapses to 1 speaker -> 503 with speaker_count_mismatch."""
    monkeypatch.setattr(main, "_run_diarization_on_wav", lambda wav, **kw: _one_speaker_result())

    resp = client.get("/healthz/synthetic")
    assert resp.status_code == 503, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["reason"] == "speaker_count_mismatch"
    assert data["expected"] == 2
    assert data["speaker_count_actual"] == 1
    assert data["segments"] == 1
    assert data["fixture"] == "synthetic_2speaker.wav"
    assert data["elapsed_ms"] >= 0


def test_healthz_synthetic_diarization_throws(client, monkeypatch):
    """Pipeline raises an exception -> 503 with diarization_threw."""

    def boom(wav, **kwargs):
        raise RuntimeError("CUDA out of memory (simulated)")

    monkeypatch.setattr(main, "_run_diarization_on_wav", boom)

    resp = client.get("/healthz/synthetic")
    assert resp.status_code == 503, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["reason"] == "diarization_threw"
    assert "RuntimeError" in data["error"]
    assert "CUDA out of memory" in data["error"]
    assert data["fixture"] == "synthetic_2speaker.wav"
    assert data["elapsed_ms"] >= 0


def test_healthz_synthetic_embedding_distance_too_low(client, monkeypatch):
    """Speaker count is right but embeddings are near-identical -> 503."""
    monkeypatch.setattr(
        main,
        "_run_diarization_on_wav",
        lambda wav, **kw: _two_speaker_too_close_embeddings(),
    )

    resp = client.get("/healthz/synthetic")
    assert resp.status_code == 503, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["reason"] == "embedding_distance_too_low"
    assert data["embedding_distance"] < data["embedding_distance_required"]
    assert data["speaker_count_actual"] == 2


def test_healthz_synthetic_segments_too_few(client, monkeypatch):
    """Probe rejects when segmenter produces a single segment for the whole clip."""
    one_segment_two_speakers = DiarizeResponse(
        segments=[
            DiarizeSegment(start=0.0, end=33.5, speaker="SPEAKER_00", embedding=_embed_vec(11)),
        ],
        # Lie about num_speakers to isolate the segments_too_few branch.
        num_speakers=2,
        backend="pyannote-community-1",
        duration_seconds=33.5,
    )
    monkeypatch.setattr(main, "_run_diarization_on_wav", lambda wav, **kw: one_segment_two_speakers)

    resp = client.get("/healthz/synthetic")
    assert resp.status_code == 503, resp.text
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["reason"] == "segments_too_few"
    assert data["segments"] == 1
