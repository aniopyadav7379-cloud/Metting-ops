"""Smoke tests for the Phase B.3 spike /transcribe-stream-v2 endpoint.

These run as standard pytests against the running service. They probe:
1. Session-state lifecycle: a new X-Session-Id creates state; X-Is-Final
   tears it down.
2. Draft-to-finalize promotion: feeding the same audio twice should
   migrate the early tokens from `tokens_draft` to `tokens_finalized`
   on subsequent calls.
3. Back-compat: the v1 /transcribe-stream endpoint still works.

Run via:
  docker exec meet-parakeet-stream-svc python -m pytest /tests/test_stream_v2.py -v

Or against a deployed instance:
  STREAM_SVC_URL=http://midboy2:8895 python -m pytest test_stream_v2.py -v
"""
from __future__ import annotations

import io
import os
import uuid
import wave
from pathlib import Path

import numpy as np
import pytest
import requests
import soundfile as sf


STREAM_SVC_URL = os.getenv("STREAM_SVC_URL", "http://localhost:8895")
FIXTURE_PATH = Path("/test_fixtures/synthetic_2speaker.wav")
TIMEOUT = 30


def _load_fixture() -> tuple[np.ndarray, int]:
    """Load the 2-speaker fixture (33.7s). Skip the test if it's missing."""
    if not FIXTURE_PATH.exists():
        pytest.skip(f"fixture {FIXTURE_PATH} not in container")
    audio, sr = sf.read(str(FIXTURE_PATH), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr


def _wav_bytes(audio: np.ndarray, sr: int = 16000) -> bytes:
    """PCM16 LE in a minimal WAV container. Matches what the meet-backend
    WS forwarder produces.
    """
    buf = io.BytesIO()
    sf.write(buf, audio, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def test_healthz_includes_b3_phase() -> None:
    """Service should report phase=B.3-spike now that v2 is wired in."""
    r = requests.get(f"{STREAM_SVC_URL}/healthz", timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.json()
    assert body.get("phase") == "B.3-spike", body


def test_v1_endpoint_still_works() -> None:
    """Phase B.2 endpoint must NOT regress."""
    audio, sr = _load_fixture()
    # Just the first 2.5s — matches what the WS handler typically posts.
    window = audio[: int(2.5 * sr)]
    body = _wav_bytes(window, sr)
    r = requests.post(
        f"{STREAM_SVC_URL}/transcribe-stream",
        data=body,
        headers={"Content-Type": "application/octet-stream",
                 "X-Session-Id": "t-v1-back-compat",
                 "X-Flush-Sequence": "0"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert "text" in out
    assert out["model"]  # something loaded
    assert out["duration"] > 0


def test_v2_requires_session_id() -> None:
    """X-Session-Id is mandatory for v2."""
    r = requests.post(
        f"{STREAM_SVC_URL}/transcribe-stream-v2",
        data=b"\x00" * 32000,
        headers={"Content-Type": "application/octet-stream"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400


@pytest.mark.parametrize("chunk_seconds", [2.5])
def test_v2_streams_draft_then_finalizes(chunk_seconds: float) -> None:
    """Feed the fixture in `chunk_seconds` chunks (default 2.5s, matching
    the meet-backend WS handler's existing cadence). We should see draft
    tokens grow, finalize on subsequent chunks, and a small final emission
    when is_final=1.

    The compute-RTF assertion is bounded by the fact that the v2 endpoint
    re-transcribes the full 6s ring on every call. For a chunk_seconds=2.5
    cadence and ring=6s, expected RTF is ~0.45 (each ~1.2s inference per
    ~2.5s chunk). We assert < 0.8 so a loaded GPU doesn't flake the test.
    """
    audio, sr = _load_fixture()
    session_id = f"t-v2-{uuid.uuid4().hex[:8]}"
    chunk_samples = int(chunk_seconds * sr)

    total_finalized: list[str] = []
    last_draft: list[str] = []
    seq = 0
    elapsed_total = 0.0
    audio_total = len(audio) / float(sr)

    pos = 0
    while pos < len(audio):
        end = min(len(audio), pos + chunk_samples)
        chunk = audio[pos:end]
        is_final = end >= len(audio)
        body = _wav_bytes(chunk, sr)
        r = requests.post(
            f"{STREAM_SVC_URL}/transcribe-stream-v2",
            data=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Session-Id": session_id,
                "X-Flush-Sequence": str(seq),
                "X-Is-Final": "1" if is_final else "0",
            },
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, r.text
        body_json = r.json()

        new_final = [t["word"] for t in body_json["tokens_finalized"]]
        last_draft = [t["word"] for t in body_json["tokens_draft"]]
        total_finalized.extend(new_final)
        elapsed_total += body_json["elapsed_ms"] / 1000.0

        print(f"seq={seq:2d} ring={body_json['ring_duration']:.2f}s "
              f"new_final={new_final} draft={last_draft}")

        seq += 1
        pos = end

    # Verifications:
    # 1. We should have at least SOME finalized tokens by the end (the
    #    fixture has ~80 words of speech).
    assert len(total_finalized) >= 10, total_finalized
    # 2. On the final call, is_final=1 finalized everything → draft empty.
    assert last_draft == []
    # 3. Streaming RTF on chunk_seconds=2.5 cadence should be < 0.8 on
    #    the 3060 — the per-call compute is ~1.2s for the 6s ring; with
    #    a 2.5s chunk we use 1.2/2.5 = 0.48 RTF per call.
    assert elapsed_total / audio_total < 0.8, (elapsed_total, audio_total)
    # 4. The accumulated finalized text should contain at least one
    #    word from each ground-truth speaker transcript.
    finalized_text = " ".join(total_finalized).lower()
    assert "welcome" in finalized_text or "first speaker" in finalized_text
    # speaker B uses "perspective"
    assert "second speaker" in finalized_text or "perspective" in finalized_text


def test_v2_session_eviction_on_is_final() -> None:
    """After is_final=1, the session should be removed from /stream-v2/sessions."""
    audio, sr = _load_fixture()
    session_id = f"t-v2-evict-{uuid.uuid4().hex[:8]}"

    # One short chunk, then close.
    chunk = audio[: int(0.5 * sr)]
    requests.post(
        f"{STREAM_SVC_URL}/transcribe-stream-v2",
        data=_wav_bytes(chunk, sr),
        headers={"Content-Type": "application/octet-stream",
                 "X-Session-Id": session_id, "X-Flush-Sequence": "0"},
        timeout=TIMEOUT,
    )
    # Session should be active.
    r = requests.get(f"{STREAM_SVC_URL}/stream-v2/sessions", timeout=TIMEOUT)
    active_ids = {s["session_id"] for s in r.json()["active_sessions"]}
    assert session_id in active_ids

    # Close it.
    chunk = audio[int(0.5 * sr):int(1.0 * sr)]
    requests.post(
        f"{STREAM_SVC_URL}/transcribe-stream-v2",
        data=_wav_bytes(chunk, sr),
        headers={"Content-Type": "application/octet-stream",
                 "X-Session-Id": session_id, "X-Flush-Sequence": "1",
                 "X-Is-Final": "1"},
        timeout=TIMEOUT,
    )
    r = requests.get(f"{STREAM_SVC_URL}/stream-v2/sessions", timeout=TIMEOUT)
    active_ids = {s["session_id"] for s in r.json()["active_sessions"]}
    assert session_id not in active_ids
