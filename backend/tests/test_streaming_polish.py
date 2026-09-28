"""Phase B.5 production-polish tests for the server-live streaming path.

Covers the four observable surfaces that ship in B.5:

  1. Backpressure: the 60-s PCM ring buffer drops oldest data when the
     incoming firehose exceeds the cap, and emits a structured warning
     so operators can see backpressure kicking in.
  2. Per-org concurrent session cap: after N open sessions on the same
     org bucket, the N+1th connect gets close 4429 + a
     ``rate_limited`` JSON error frame.
  3. Prometheus counters increment for the connection accept path, the
     audio-forward path, the partials path, and the close-codes path.
  4. Parakeet slow-upstream handling: when /transcribe-stream takes
     longer than ``STREAM_SLOW_THRESHOLD_S``, ``state.skip_next_n``
     gets set so the next few cadence ticks short-circuit instead of
     piling up forwards onto an already-slow GPU.

The tests work on the SQLite test fixture; the WS endpoint is exercised
via ``TestClient.websocket_connect``. We monkeypatch ``_resolve_ws_user``
to short-circuit the oauth2-proxy header dance + DB lookup the same way
test_streaming_tier_gate.py does.

For tests that need to drive the metric counters directly (without an
end-to-end WS), we read the counters off ``api.streaming`` and observe
their value before / after.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest
from starlette.websockets import WebSocketDisconnect


# ---------------------------------------------------------------------------
# Lightweight User stand-in (matches what auth/tier reads off the User)
# ---------------------------------------------------------------------------


@dataclass
class _FakeWSUser:
    email: str = "polish@meeting-ops.local"
    tier: Optional[str] = "pro"
    is_superuser: bool = False
    is_active: bool = True
    # Stable org id so the per-org rate-limit bucket is deterministic.
    organization_id: str = "org-polish"


@pytest.fixture()
def patch_resolver(monkeypatch):
    """Mirror the same fixture as test_streaming_tier_gate. Pro-tier user
    by default; pass a different fake to override."""
    from api import streaming as streaming_mod

    state = {"user": _FakeWSUser()}

    def fake_resolver(_websocket):
        return state["user"]

    monkeypatch.setattr(streaming_mod, "_resolve_ws_user", fake_resolver)

    def setter(user):
        state["user"] = user

    return setter


@pytest.fixture(autouse=True)
def reset_session_state(monkeypatch):
    """Clear the active_sessions registry + org counter between tests so a
    test that opens connections doesn't leak state into the next test.

    Also reset the slow-upstream env so tests don't trip on each other's
    monkeypatched thresholds.
    """
    from api import streaming as streaming_mod

    streaming_mod.active_sessions.clear()
    streaming_mod._org_session_counts.clear()
    yield
    streaming_mod.active_sessions.clear()
    streaming_mod._org_session_counts.clear()


# ---------------------------------------------------------------------------
# Test 1: backpressure on the PCM buffer
# ---------------------------------------------------------------------------


def test_pcm_buffer_drops_oldest_at_60s_cap(caplog):
    """Pushing >60 s of audio into _SessionState should trim the oldest
    chunks and log a structured backpressure warning. We test the unit
    directly (no WS dance) because the trim logic is deterministic and
    we want to assert exact byte counts."""
    import logging

    from api.streaming import (
        MAX_PCM_BUFFER_SECONDS,
        _SessionState,
    )

    state = _SessionState(session_id="bp-1", user_email="bp@m.local")
    sr = 16000
    # 1 second of PCM16 mono = 32_000 bytes.
    one_second = b"\x00\x01" * sr

    caplog.set_level(logging.WARNING, logger="api.streaming")

    # Push 70 chunks of 1 s = 70 s of audio. Cap is 60 s, so we expect
    # ~10 s worth (320_000 bytes) dropped.
    for _ in range(70):
        state.append_pcm(one_second, sr)

    assert state.cumulative_audio_ms == 70_000
    expected_max = int(MAX_PCM_BUFFER_SECONDS * sr * 2)
    actual_buf_bytes = sum(len(c) for c in state.pcm_chunks)
    assert actual_buf_bytes <= expected_max, (actual_buf_bytes, expected_max)
    # We should have dropped at least 9 seconds worth (allow for the
    # one-chunk-keep invariant in append_pcm).
    assert state.bytes_dropped_backpressure >= 9 * sr * 2, state.bytes_dropped_backpressure

    # And we should see at least one structured warning log.
    backpressure_logs = [
        r for r in caplog.records
        if "[streaming-backpressure]" in r.getMessage()
        and "dropped" in r.getMessage()
    ]
    assert len(backpressure_logs) >= 1, [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Test 2: per-org rate limit triggers close 4429
# ---------------------------------------------------------------------------


def test_per_org_concurrent_session_cap(client, patch_resolver, monkeypatch):
    """When the per-org concurrent-session count is at the cap, a new
    connect must close 4429 with a `rate_limited` JSON error frame.

    Starlette's TestClient doesn't model truly concurrent WS connections
    well (the in-process synchronous WS loop serializes them), so rather
    than holding N real sockets open we directly pre-seed
    ``_org_session_counts`` to "at cap" and verify the gate fires. The
    end-to-end gate logic is the same code path; we're just skipping the
    irrelevant juggling of multiple TestClient sessions.
    """
    from api import streaming as streaming_mod

    monkeypatch.setattr(streaming_mod, "STREAMING_MAX_SESSIONS_PER_ORG", 2)
    patch_resolver(_FakeWSUser(tier="pro", organization_id="org-rl"))

    # Pretend two sessions are already live for this org.
    bucket = "org:org-rl"
    streaming_mod._org_session_counts[bucket] = 2

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/sessions/rl-c/live") as ws:
            err = ws.receive_json()
            assert err["type"] == "error", err
            assert err["reason"] == "rate_limited", err
            assert err["max_sessions_per_org"] == 2, err
            assert err["active_sessions"] == 2, err
            # Drain the close frame so the disconnect raises with the
            # right code.
            ws.receive_json()
    assert exc_info.value.code == 4429, exc_info.value.code
    # The reject path must NOT have bumped the bucket count - rejected
    # connects don't get registered.
    assert streaming_mod._org_session_counts[bucket] == 2


# ---------------------------------------------------------------------------
# Test 3a: ws_connections_total counter increments for accepted connects
# ---------------------------------------------------------------------------


def test_connection_metric_counts_accepted(client, patch_resolver):
    """A successful pro-tier connect must bump
    ws_connections_total{tier='pro',result='accepted'} by exactly 1."""
    from api.streaming import ws_connections_total

    patch_resolver(_FakeWSUser(tier="pro", organization_id="org-metric-a"))

    before = ws_connections_total.labels(tier="pro", result="accepted")._value.get()

    url = "/ws/sessions/metric-accept/live"
    with client.websocket_connect(url) as ws:
        ws.receive_json()  # drain ready
        ws.send_text('{"type":"end"}')
        ws.receive_json()  # drain closing

    after = ws_connections_total.labels(tier="pro", result="accepted")._value.get()
    assert after - before == 1, (before, after)


# ---------------------------------------------------------------------------
# Test 3b: ws_connections_total counter for rate-limit rejection
# ---------------------------------------------------------------------------


def test_connection_metric_counts_rate_limited(client, patch_resolver, monkeypatch):
    """A rate-limited reject must bump
    ws_connections_total{tier='pro',result='rate_limited'} by 1
    and ws_close_codes_total{code='4429'} by 1.

    Same pre-seed trick as the rate-limit test: we set the org count to
    the cap directly rather than holding a real socket open.
    """
    from api import streaming as streaming_mod

    monkeypatch.setattr(streaming_mod, "STREAMING_MAX_SESSIONS_PER_ORG", 1)
    patch_resolver(_FakeWSUser(tier="pro", organization_id="org-metric-rl"))

    rl_counter = streaming_mod.ws_connections_total.labels(
        tier="pro", result="rate_limited"
    )
    cc_counter = streaming_mod.ws_close_codes_total.labels(code="4429")
    rl_before = rl_counter._value.get()
    cc_before = cc_counter._value.get()

    streaming_mod._org_session_counts["org:org-metric-rl"] = 1

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/sessions/metric-rl-b/live") as ws_b:
            ws_b.receive_json()  # error frame
            ws_b.receive_json()  # close

    rl_after = rl_counter._value.get()
    cc_after = cc_counter._value.get()
    assert rl_after - rl_before == 1, (rl_before, rl_after)
    assert cc_after - cc_before == 1, (cc_before, cc_after)


# ---------------------------------------------------------------------------
# Test 4: parakeet slow-upstream sets skip_next_n
# ---------------------------------------------------------------------------


def test_slow_upstream_triggers_skip_next_n(monkeypatch):
    """If a single flush takes longer than STREAM_SLOW_THRESHOLD_S, the
    next few cadence ticks must short-circuit via skip_next_n.

    We exercise `_flush_to_stt` directly with a mock httpx client that
    fakes a slow response, then assert state.skip_next_n was bumped.

    Then we call should_flush() three times to confirm the skip ladder
    drains correctly (False, False, True if there's enough audio).

    Async style matches the existing test_brigade_writer.py pattern of
    asyncio.run(...).
    """
    import asyncio as _asyncio

    from api import streaming as streaming_mod

    # Tighten threshold to make the test fast.
    monkeypatch.setattr(streaming_mod, "STREAM_SLOW_THRESHOLD_S", 0.01)
    monkeypatch.setattr(streaming_mod, "STREAM_SKIP_NEXT_ON_SLOW", 2)

    state = streaming_mod._SessionState(session_id="slow-1", user_email="slow@m.local")
    sr = 16000
    state.append_pcm(b"\x00\x01" * sr * 3, sr)  # 3 s

    class _SlowResp:
        status_code = 200

        def json(self):
            return {"text": "ok", "segments": [], "model": "fake", "rtf": 0.1}

        @property
        def text(self):
            return ""

    class _SlowClient:
        async def post(self, *_args, **_kwargs):
            # Simulate slow upstream just above the threshold.
            await _asyncio.sleep(0.05)
            return _SlowResp()

    class _FakeWS:
        def __init__(self):
            self.sent: list = []

        async def send_json(self, payload):
            self.sent.append(payload)

    fake_ws = _FakeWS()
    # asyncio.run() gives us a fresh loop independent of whatever state the
    # surrounding suite left behind, and is the supported replacement for
    # the deprecated get_event_loop()/new_event_loop() dance (the latter
    # left the whole-suite run with a closed loop on Python 3.14).
    _asyncio.run(
        streaming_mod._flush_to_stt(
            state, fake_ws,  # type: ignore[arg-type]
            is_final=False,
            http_client=_SlowClient(),  # type: ignore[arg-type]
        )
    )

    assert state.skip_next_n == 2, state.skip_next_n
    # Confirm we sent a partial back (the slow path still succeeded).
    assert any(p.get("type") == "partial" for p in fake_ws.sent), fake_ws.sent

    # Now exercise the skip ladder via should_flush. We need to seed
    # more audio so the cumulative delta is past the 2.5 s threshold.
    state.append_pcm(b"\x00\x01" * sr * 3, sr)  # 3 more seconds
    # should_flush() should burn down skip_next_n twice, then True.
    assert state.should_flush() is False
    assert state.skip_next_n == 1

    state.append_pcm(b"\x00\x01" * sr * 3, sr)
    assert state.should_flush() is False
    assert state.skip_next_n == 0

    state.append_pcm(b"\x00\x01" * sr * 3, sr)
    assert state.should_flush() is True


# ---------------------------------------------------------------------------
# Test 5: active_sessions registry tracks live connections
# ---------------------------------------------------------------------------


def test_active_sessions_registry_tracks_connections(client, patch_resolver):
    """While a WS session is open, `active_sessions[session_id]` is set;
    after the client closes, it's removed. Critical for the SIGTERM
    drain hook in main.py."""
    from api import streaming as streaming_mod

    patch_resolver(_FakeWSUser(tier="pro", organization_id="org-active"))

    sid = "active-1"
    assert sid not in streaming_mod.active_sessions

    with client.websocket_connect(f"/ws/sessions/{sid}/live") as ws:
        ws.receive_json()  # ready
        # Now the registry should contain this session.
        assert sid in streaming_mod.active_sessions, list(streaming_mod.active_sessions.keys())
        ws.send_text('{"type":"end"}')
        ws.receive_json()  # closing

    # After close, the finally block must have unregistered.
    assert sid not in streaming_mod.active_sessions


# ---------------------------------------------------------------------------
# Test 6: org bucket count drains correctly on disconnect
# ---------------------------------------------------------------------------


def test_org_session_count_drains_on_disconnect(client, patch_resolver):
    """Opening + closing a session must leave the per-org count at 0.

    This is important: without proper drainage, a buggy reconnect storm
    would exhaust the per-org cap permanently and lock out the user
    until the backend restarts.
    """
    from api import streaming as streaming_mod

    patch_resolver(_FakeWSUser(tier="pro", organization_id="org-drain"))

    # Bucket should not exist before any connect.
    assert streaming_mod._org_session_counts.get("org:org-drain", 0) == 0

    with client.websocket_connect("/ws/sessions/drain-1/live") as ws:
        ws.receive_json()
        assert streaming_mod._org_session_counts["org:org-drain"] == 1
        ws.send_text('{"type":"end"}')
        ws.receive_json()

    # The finally block should have decremented us back to 0 / removed.
    assert streaming_mod._org_session_counts.get("org:org-drain", 0) == 0


# ---------------------------------------------------------------------------
# Test 7: /metrics endpoint exposes Prometheus text format
# ---------------------------------------------------------------------------


def test_metrics_endpoint_serves_prometheus_text(client):
    """/metrics must be mounted and return Prometheus text format with
    at least one of our B.5 counters showing up by name."""
    resp = client.get("/metrics")
    assert resp.status_code == 200, resp.status_code
    body = resp.text
    # The histogram + counters should appear in the registry text.
    assert "meeting_ops_ws_connections_total" in body, body[:500]
    assert "meeting_ops_parakeet_stream_request_duration_seconds" in body, body[:500]
    assert "meeting_ops_ws_close_codes_total" in body, body[:500]
