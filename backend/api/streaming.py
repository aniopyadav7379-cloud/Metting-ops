"""Phase B.2 server-live streaming WebSocket endpoint (tier-gated in B.4).

This module implements the meet-backend side of the Phase B server-live
audio pipeline described in `docs/phase-b-server-live-streaming.md`.

B.2 ships:
  - real per-session audio buffering (PCM16 ring),
  - periodic forwarding (every ~2.5 s of speech OR on ``{"type": "flush"}``)
    of the buffered audio to ``meet-parakeet-stream-svc`` on midboy2,
  - emission of partial / final transcript frames back to the WS client,
  - control-frame ``end`` triggers one last flush + a final transcript.

B.4 layers on top:
  - tier gate on the ``server_live`` capability. Free-tier users get a
    JSON ``{"type": "error", "reason": "tier_insufficient", ...}`` frame
    followed by close code 4003 (RFC 6455 application-specific range).
    Pro / Enterprise / superuser-overridden proceed normally.

This is **near-streaming**: chunked offline transcription over short
windows. True NeMo streaming (draft + finalize tokens) lands in B.3.
The wire contract from Section 4 of the design doc is forward-compatible
with the B.3 protocol; the only thing that changes is that the partials
will come faster + finer-grained when the streaming model is wired in.

Endpoint
--------
``ws[s]://<host>/ws/sessions/{session_id}/live``

Auth
----
oauth2-proxy in front of meet-backend forwards ``X-Auth-Request-Email`` /
``X-Auth-Request-Preferred-Username`` headers to upstream on every request,
including the HTTP upgrade that initiates a WebSocket. We inspect those
headers off ``websocket.headers``, look up the user, and reject with close
code 4001 if no user matches.

Tier gate (B.4)
---------------
After the user is resolved (DB row, not just email), we check
``get_tier_features(user)['server_live']``. If False:

  - ``await websocket.accept()`` so we can send a clean payload,
  - emit ``{"type": "error", "reason": "tier_insufficient", ...}``,
  - close with code 4003, reason "tier_insufficient".

``get_user_tier()`` (in ``auth/tier.py``) treats ``is_superuser=True``
as enterprise regardless of the tier column, so support/dev accounts
keep server-live access without per-tier toggling.

Frame protocol (canonical as of B.2 reconciliation 2026-05-22)
--------------------------------------------------------------
The 19-byte fixed big-endian header carries every binary audio frame::

    bytes 0-3   uint32 BE  sequence_number   (per-stream monotonic)
    bytes 4-11  uint64 BE  client_timestamp_us
    bytes 12-15 4-char ASCII  payload_format  ("PC16" = 16 kHz mono PCM16)
    bytes 16-17 uint16 BE  sample_rate / 100  (160 = 16000 Hz)
    byte 18     uint8       flags  (bit 0: is_final)
    bytes 19..N audio payload (PCM16 little-endian by convention)

The design doc originally drafted a variable-length little-endian header
with a leading version byte; Phase B.2 reconciled to this fixed
big-endian form because it's simpler to parse correctly in both Python
and TypeScript and carries identical information. See section 4 of
``docs/phase-b-server-live-streaming.md`` for the full rationale.

Control frames (text/JSON)
--------------------------
- ``{"type": "end"}``    — client done; one final flush + close.
- ``{"type": "flush"}``  — force-transcribe the current buffer now.
- ``{"type": "ping"}``   — keepalive; server replies with ``pong``.
- anything else          — echoed as ``ack_control`` (forward-compat).

Server-emitted frames
---------------------
- ``{"type": "ready", ...}``         — sent immediately after accept.
- ``{"type": "ack",   ...}``         — per binary frame (header echo).
- ``{"type": "partial", ...}``       — chunked transcript while audio flows.
- ``{"type": "final", ...}``         — last transcript on session end.
- ``{"type": "ack_control", ...}``   — per unknown control frame.
- ``{"type": "error", ...}``         — per malformed frame / upstream fail.
- ``{"type": "pong"}``               — per ``{"type":"ping"}`` control.
- ``{"type": "closed", ...}``        — server-initiated close.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from collections import defaultdict

# v3.18.3: audioop was removed from the Python 3.13/3.14 stdlib (PEP 594).
# Fall back to the PyPI `audioop-lts` back-port when stdlib is gone; if
# neither is available, run in degraded mode and report via /health.
try:
    import audioop  # type: ignore
except ImportError:  # pragma: no cover - py3.13+ path
    try:
        import audioop_lts as audioop  # type: ignore
    except ImportError:
        audioop = None  # type: ignore[assignment]
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Histogram
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import DetachedInstanceError

from auth.models import User
from auth.service import AuthService
from auth.tier import get_tier_features, get_user_tier, org_covers_feature
from database.database import SessionLocal

logger = logging.getLogger(__name__)

if audioop is None:
    logger.warning(
        "audioop unavailable (stdlib removed in py3.13+, and audioop-lts "
        "not installed). Streaming VAD silence-gate runs in degraded mode "
        "(no audio-amplitude check); install `audioop-lts` to restore."
    )

router = APIRouter(tags=["streaming"])

# ---------------------------------------------------------------------------
# Phase B.5 Prometheus metrics
# ---------------------------------------------------------------------------
#
# Exposed via /metrics in backend/main.py. Labels stay low-cardinality so
# the Prometheus scrape doesn't blow up. We don't put session_id on any
# label - it's per-connection unique. Tier values are bounded by
# TIER_FEATURES (free/pro/enterprise), result values are enumerated below.

ws_connections_total = Counter(
    "meeting_ops_ws_connections_total",
    "WebSocket connection attempts on /ws/sessions/{id}/live",
    ["tier", "result"],  # tier=free/pro/enterprise/unknown,
                          # result=accepted/tier_rejected/auth_rejected/rate_limited
)

ws_audio_frames_forwarded_total = Counter(
    "meeting_ops_ws_audio_frames_forwarded_total",
    "Audio frames forwarded to parakeet-stream-svc",
)

ws_partial_transcripts_emitted_total = Counter(
    "meeting_ops_ws_partial_transcripts_emitted_total",
    "Partial transcripts emitted back to clients",
)

ws_close_codes_total = Counter(
    "meeting_ops_ws_close_codes_total",
    "WebSocket close codes seen",
    ["code"],
)

parakeet_stream_request_duration_seconds = Histogram(
    "meeting_ops_parakeet_stream_request_duration_seconds",
    "Round-trip time for parakeet-stream-svc /transcribe-stream calls",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)


# ---------------------------------------------------------------------------
# Phase B.5 backpressure + rate limit state
# ---------------------------------------------------------------------------
#
# Active sessions: registry of currently-open WS handlers, indexed by
# session_id. Used by the SIGTERM drain hook in main.py to send a
# "server_shutdown" frame + close-1001 to each active client before exit.
# Also used by the per-org rate-limit logic to count concurrent live
# sessions per org.

active_sessions: dict[str, WebSocket] = {}
# Per-org concurrent session count. Indexed by str(org_id) - org_id can be
# None for users without an org assignment, in which case we use the
# user email as the bucket so the limit still applies somewhere meaningful.
_org_session_counts: dict[str, int] = defaultdict(int)
# Slow-parakeet threshold. If a flush takes longer than this, we skip the
# next N forwards to give the GPU breathing room.
STREAM_SLOW_THRESHOLD_S = float(os.getenv("STREAM_SLOW_THRESHOLD_S", "2.0"))
STREAM_SKIP_NEXT_ON_SLOW = int(os.getenv("STREAM_SKIP_NEXT_ON_SLOW", "2"))
# Per-org concurrent live-session cap. Excess connections get close 4429.
STREAMING_MAX_SESSIONS_PER_ORG = int(
    os.getenv("STREAMING_MAX_SESSIONS_PER_ORG", "5")
)


def _resolve_org_bucket(user: User) -> str:
    """Pick a stable bucket key for per-org rate limiting.

    Prefer the user's organization id if available (auth.models.User may
    expose it via .organization_id, .org_id, .organizations[0].id, etc.,
    depending on the schema). Fall back to the email so the limit still
    has a meaningful unit even for users without an org.

    Wrapped in a DetachedInstanceError-safe try/except because
    ``_resolve_ws_user`` closes its DB session before returning the User
    object (Starlette's WebSocket handlers don't compose cleanly with
    ``Depends(get_db)`` yield-style sessions). Accessing the unloaded
    ``organizations`` relationship on a detached User triggers a
    lazy-load that raises DetachedInstanceError — falling back to the
    email-based bucket is the correct behavior in that case.
    """
    for attr in ("organization_id", "org_id"):
        val = getattr(user, attr, None)
        if val:
            return f"org:{val}"
    try:
        orgs = getattr(user, "organizations", None)
    except DetachedInstanceError:
        orgs = None
    if orgs:
        try:
            first = orgs[0]
            oid = getattr(first, "id", None) or getattr(first, "organization_id", None)
            if oid:
                return f"org:{oid}"
        except (IndexError, TypeError, AttributeError, DetachedInstanceError):
            pass
    return f"user:{user.email}"

# Frame layout — see module docstring.
HEADER_SIZE = 19
HEADER_STRUCT = ">IQ4sHB"
PROTOCOL_VERSION = "0.2.0"  # bumped from 0.1.0-scaffold (B.1) for real forwarding.

# Where the streaming STT service lives. midboy2 over Tailscale by default,
# matching the existing PARAKEET_SERVER_URL convention. Overridable via env
# so dev / smoke-test setups can point at a local stub.
PARAKEET_STREAM_URL = os.getenv(
    "PARAKEET_STREAM_URL",
    "http://meet-parakeet-stream-svc:8895",
).rstrip("/")

# Window flush cadence. Every 2.5 s of audio (or on explicit flush) we
# build a WAV from the accumulated PCM16 frames and POST it. Shorter
# windows give snappier partials at the cost of more upstream load;
# longer windows give better word-level continuity. 2.5 s is the
# starting compromise; B.3 will replace this with NeMo native streaming.
FLUSH_AUDIO_SECONDS = float(os.getenv("STREAM_FLUSH_AUDIO_SECONDS", "2.5"))

# Upper bound on retained PCM audio per session. We never need more than
# this on the live path — the canonical transcript comes from the chunks
# upload pipeline that runs in parallel. 60 s is generous; the live
# rolling window is intentionally short.
MAX_PCM_BUFFER_SECONDS = 60.0

# Phase B.3 polish: lookback window for word-boundary continuity.
# Each flush sends audio from (consumed_through_ms - LOOKBACK) forward so
# the model sees a tiny overlap with the previous window — improves word
# boundaries at flush edges without re-transcribing the entire prior
# window. 1.0 s is the speech-rate-aware sweet spot (covers most word
# boundaries; matches NeMo's recommended chunked-streaming lookback).
STREAM_LOOKBACK_SECONDS = float(os.getenv("STREAM_LOOKBACK_SECONDS", "1.0"))

# Phase B.3 polish: VAD silence threshold via audioop.rms() on PCM16.
# RMS scale: 0 = silence, 32767 = full-scale sine peak. Typical speech is
# 1000-5000 RMS; quiet background noise is ~50-200; true silence is < 50.
# Threshold of 200 catches most silence while letting quiet speech through.
# Skipped flushes still advance the consumed cursor so silence doesn't
# accumulate as a backlog of re-transcription targets.
STREAM_VAD_RMS_THRESHOLD = int(os.getenv("STREAM_VAD_RMS_THRESHOLD", "200"))
STREAM_VAD_ENABLED = os.getenv("STREAM_VAD_ENABLED", "1") not in (
    "0", "false", "False", "no", "",
)

# Phase B.3 polish: drop sortformer speaker turns shorter than this duration
# before emitting in the partial JSON. Sortformer occasionally over-segments
# on solo audio — flagging brief background-noise / breath sounds as a
# different speaker for ~100-300 ms. Real conversational turns are almost
# always > 500 ms (people speak for at least a syllable). Drops the noise
# without affecting legitimate fast-cut speaker switches.
SORTFORMER_MIN_SEGMENT_MS = int(os.getenv("SORTFORMER_MIN_SEGMENT_MS", "500"))


def _is_silent(pcm: bytes, threshold: int = STREAM_VAD_RMS_THRESHOLD) -> bool:
    """RMS-based silence detection on PCM16 mono audio.

    audioop.rms returns the root-mean-square amplitude as an int. For
    silence detection we don't need fancy VAD (webrtcvad / silero) —
    a fixed RMS threshold catches the silence-hallucination cases
    (parakeet was trained with full-context attention and fills quiet
    windows with repeating tokens like "a little bit of a little bit of...").

    Returns True when the window's RMS amplitude is below threshold,
    which means we should skip the parakeet call entirely.
    """
    if not pcm:
        return True
    if audioop is None:
        # v3.18.3: degraded mode — without audioop we can't compute RMS, so
        # treat as non-silent and let upstream parakeet take the call. This
        # keeps streaming functional on py3.14 if audioop-lts isn't installed.
        return False
    try:
        return audioop.rms(pcm, 2) < threshold
    except audioop.error:
        # Bad PCM length / odd-byte buffer — treat as non-silent so we
        # don't silently drop data; the upstream model can decide.
        return False

# Upstream HTTP timeouts. The model on midboy2 takes ~200-600 ms for a
# 2.5 s window at fp16 on the 3060 once warmed; cold start is up to
# ~30 s while NeMo loads. We give /transcribe-stream 30 s of grace
# during warm-up and 10 s once warm — both controllable via env.
STREAM_UPSTREAM_TIMEOUT_S = float(os.getenv("STREAM_UPSTREAM_TIMEOUT_S", "30.0"))

# ---------------------------------------------------------------------------
# Phase B.3 integration flags
# ---------------------------------------------------------------------------
# All default OFF — production behavior is identical to v1.0.0/v1.1.0. Flip
# per env after the corresponding B.3 chunk lands and is verified.
# See docs/phase-b3-integration-plan.md for the integration target spec.
#
# These are read at module import; the integration PR that follows B.3 agent
# completion will reference them in _flush_to_stt() to route audio per flag.
STREAMING_USE_V2_PARAKEET = os.getenv("STREAMING_USE_V2_PARAKEET", "0") not in (
    "0", "false", "False", "no", "",
)
STREAMING_USE_SORTFORMER = os.getenv("STREAMING_USE_SORTFORMER", "0") not in (
    "0", "false", "False", "no", "",
)
# Where the streaming Sortformer service will live once Phase B.3 chunk D
# ships. midboy2 over Tailscale, parallel port to parakeet-stream-svc (8895)
# and parakeet-svc (8881). Overridable via env so dev / smoke-test setups
# can point at a local stub.
SORTFORMER_URL = os.getenv(
    "SORTFORMER_URL",
    "http://meet-sortformer-svc:8896",
).rstrip("/")


def _resolve_ws_user_email(websocket: WebSocket) -> Optional[str]:
    """Read oauth2-proxy forward-auth email off the WebSocket upgrade headers.

    Mirrors the trust order in ``auth.dependencies.get_current_user_optional``
    so a user authenticated against the REST API the same way is recognised
    here. Returns the first non-empty email-shaped value, else None.

    SECURITY: like the REST path, the forward-auth identity headers are only
    honoured when the WS upgrade crossed the trusted proxy (X-Proxy-Auth
    secret). A forged header on a direct-to-container upgrade must not
    authenticate anyone. Fails open until the secret is configured.
    """
    from auth.proxy_trust import forward_auth_trusted
    if not forward_auth_trusted(websocket.headers):
        return None
    candidates = [
        websocket.headers.get("x-auth-request-email"),
        websocket.headers.get("x-forwarded-email"),
        websocket.headers.get("x-auth-request-preferred-username"),
        websocket.headers.get("x-forwarded-preferred-username"),
        websocket.headers.get("x-auth-request-user"),
        websocket.headers.get("x-forwarded-user"),
    ]
    for raw in candidates:
        if not raw:
            continue
        value = raw.strip()
        if value:
            return value
    return None


def _resolve_ws_user(websocket: WebSocket) -> Optional[User]:
    """Resolve a DB User from oauth2-proxy headers on the WS upgrade.

    Returns None if no email-shaped header is present OR if the email
    doesn't map to an active User row. Looks the user up directly so the
    caller has access to the tier/is_superuser columns for the B.4 gate.

    We open + close our own ``SessionLocal()`` since ``Depends(get_db)``
    doesn't compose cleanly with WebSocket handlers (Starlette doesn't
    run dependencies that yield for a WS upgrade). The session is closed
    before we return — we hold the User object, not the session, for the
    rest of the WS lifecycle.

    Overridable for tests via ``app.dependency_overrides`` on the module-
    level callable (see ``test_streaming_tier_gate.py``).
    """
    email = _resolve_ws_user_email(websocket)
    if not email:
        return None

    db: Session = SessionLocal()
    try:
        # Lookup is case-insensitive on email; mirror service.get_user_by_email.
        user = AuthService.get_user_by_email(db, email)
        if user and user.is_active:
            # Materialise org membership before detaching so the cross-tenant
            # session check (ws_user_can_access_session) and the per-workspace
            # billing gate work after the session closes — mirrors
            # auth.ws_auth.authenticate_ws. Without this, _org_ids is empty and
            # every org-bound session would be wrongly rejected.
            from auth.models import UserOrganization
            org_ids = [
                row[0]
                for row in db.query(UserOrganization.organization_id)
                .filter(UserOrganization.user_id == user.id)
                .all()
            ]
            db.expunge(user)
            user._org_ids = org_ids
            return user
        return None
    except Exception:
        logger.exception("[B.4 WS] user lookup failed email=%s", email)
        return None
    finally:
        db.close()


def _resolve_ws_session_org(session_id: str):
    """The Organization that owns this streaming session (the active workspace).

    Thin wrapper over ``auth.ws_auth.resolve_session_org`` so the per-workspace
    billing gate reads the session-owning org's plan (gate on the active
    workspace's entitlement, not the user's global tier). None when unresolved
    -> caller falls back to the user-tier gate alone.
    """
    from auth.ws_auth import resolve_session_org
    return resolve_session_org(session_id)


@dataclass
class _SessionState:
    """Per-WS server-side state.

    The PCM buffer is a list of raw PCM16 little-endian frames the client
    sent. We concatenate when it's time to flush (no need to keep a
    contiguous bytearray hot — this is run rarely).

    last_seq is the highest sequence number we've received; the client
    can use ``ack`` echoes to detect drops. last_flush_at_audio_ms tracks
    cumulative audio (in ms) at the time of the last upstream call so we
    can fire when ``cumulative - last_flush >= FLUSH_AUDIO_SECONDS * 1000``.

    Phase B.5 additions:
      - ``bytes_dropped_backpressure`` records how much PCM we dropped due
        to the 60 s cap, exposed in close-frame diagnostics.
      - ``skip_next_n`` counts down forwards to skip because the previous
        flush exceeded ``STREAM_SLOW_THRESHOLD_S``. While > 0, ``should_flush``
        keeps returning False until the GPU catches up.
      - ``org_bucket`` is the key into ``_org_session_counts`` so we can
        decrement on disconnect.
    """
    session_id: str
    user_email: str
    org_bucket: str = ""
    # v3.20.1: per-user gate on the sortformer dispatch below. Even when the
    # instance-wide ``STREAMING_USE_SORTFORMER`` env is on, we only fire
    # sortformer for users whose effective tier features include
    # ``live_diarization`` (enterprise + Founding 100). Pro users get the
    # streaming transcript without live speaker labels; their completion
    # pyannote pass at session end still gives them full speaker attribution.
    live_diarization_allowed: bool = False
    sample_rate: int = 16000  # filled from first audio frame
    pcm_chunks: list[bytes] = field(default_factory=list)
    cumulative_audio_ms: int = 0
    last_flush_at_audio_ms: int = 0
    # Phase B.3 polish: cursor tracking how much audio has been emitted as
    # a partial / final. Each flush sends audio from
    # (consumed_through_ms - LOOKBACK) forward so we never re-transcribe
    # previously-emitted content. Advances on every flush (successful OR
    # silence-skipped) so silence doesn't accumulate as a re-transcribe
    # backlog.
    consumed_through_ms: int = 0
    last_seq: int = 0
    flush_counter: int = 0  # increments per upstream transcribe call
    last_partial_text: str = ""  # de-dup empty/identical partials
    in_flight: bool = False  # one upstream call at a time per session
    bytes_dropped_backpressure: int = 0  # cumulative PCM dropped due to 60s cap
    skip_next_n: int = 0  # remaining flushes to skip after a slow upstream

    def append_pcm(self, payload: bytes, sample_rate: int) -> None:
        """Append a PCM16 payload and update the cumulative-audio bookkeeping.

        Phase B.5: enforces the 60 s cap as a hard ceiling. If the
        incoming payload would push us over, we drop the oldest data and
        log a structured warning so operators can see backpressure
        kicking in.
        """
        if not payload:
            return
        self.sample_rate = sample_rate or self.sample_rate
        # PCM16 = 2 bytes per sample, mono.
        samples = len(payload) // 2
        ms = int(round((samples / float(self.sample_rate)) * 1000.0))
        self.pcm_chunks.append(payload)
        self.cumulative_audio_ms += ms

        # Trim the front of the buffer if we'd exceed MAX_PCM_BUFFER_SECONDS.
        # We keep flushes small + recent; older audio is the chunks-upload
        # pipeline's job.
        max_bytes = int(MAX_PCM_BUFFER_SECONDS * self.sample_rate * 2)
        total = sum(len(c) for c in self.pcm_chunks)
        dropped_this_call = 0
        while total > max_bytes and len(self.pcm_chunks) > 1:
            dropped = self.pcm_chunks.pop(0)
            total -= len(dropped)
            dropped_this_call += len(dropped)

        if dropped_this_call > 0:
            self.bytes_dropped_backpressure += dropped_this_call
            logger.warning(
                "[streaming-backpressure] session=%s dropped %d bytes "
                "(buffer exceeded %ds cap, total_dropped=%d)",
                self.session_id, dropped_this_call,
                int(MAX_PCM_BUFFER_SECONDS), self.bytes_dropped_backpressure,
            )

    def should_flush(self) -> bool:
        """True when we have at least FLUSH_AUDIO_SECONDS of new audio since the
        last flush. Uses cumulative bookkeeping, not buffer size, so a slow
        client doesn't gate us on calendar time.

        Phase B.5: respects ``skip_next_n``. When the previous flush took
        longer than ``STREAM_SLOW_THRESHOLD_S`` we decrement skip_next_n
        instead of flushing, giving the GPU breathing room. We still
        advance ``last_flush_at_audio_ms`` so we don't accumulate a
        firehose of pending audio while we wait."""
        delta_ms = self.cumulative_audio_ms - self.last_flush_at_audio_ms
        if delta_ms < int(FLUSH_AUDIO_SECONDS * 1000):
            return False
        if self.skip_next_n > 0:
            self.skip_next_n -= 1
            # Advance the watermark so we don't run away with backlog -
            # the trim-to-25 s window in take_pcm keeps the actual buffer
            # bounded; this just keeps the cadence honest.
            self.last_flush_at_audio_ms = self.cumulative_audio_ms
            logger.info(
                "[streaming-backpressure] session=%s skipping flush "
                "(skip_next_n remaining=%d, audio_ms=%d)",
                self.session_id, self.skip_next_n, self.cumulative_audio_ms,
            )
            return False
        return True

    def take_pcm(
        self,
        max_seconds: float = 25.0,
        lookback_seconds: float = STREAM_LOOKBACK_SECONDS,
    ) -> bytes:
        """Return audio from ``consumed_through_ms - lookback_seconds`` forward.

        Phase B.3 polish: previously returned the entire 25 s rolling window
        every flush, which forced parakeet to re-transcribe the same audio
        each time. With ``att_context_size=[-1,-1]`` full-context training,
        that produced spurious repetition + silence hallucination ("Okay, so
        I just hit..." would repeat, then degenerate into "a little bit of
        a little bit of a little bit of...") because the model had no signal
        for "stop emitting tokens past this point".

        New behavior: each flush sends audio between (consumed_through_ms -
        lookback_seconds) and cumulative_audio_ms — i.e. only NEW audio plus
        a small overlap for word-boundary continuity. The caller is
        responsible for advancing ``consumed_through_ms`` after a successful
        flush (or after a silence-skipped flush, so the cursor never
        falls behind).

        Still caps to ``max_seconds`` as a safety net for recovery from
        long backpressure stalls (if N flushes failed, we don't send 5×N
        seconds of audio on the next try).
        """
        joined = b"".join(self.pcm_chunks)
        if not joined:
            return b""

        # Map ms boundaries to byte offsets in the joined buffer.
        # Buffer's first byte corresponds to (cumulative_audio_ms - buffer_duration_ms).
        bytes_per_ms = self.sample_rate * 2 / 1000.0
        buffer_duration_ms = int(len(joined) / bytes_per_ms)
        buffer_start_ms = self.cumulative_audio_ms - buffer_duration_ms

        # Compute the window we want to send.
        lookback_ms = int(lookback_seconds * 1000)
        start_ms = max(buffer_start_ms, self.consumed_through_ms - lookback_ms)

        # Cap window length to max_seconds (recovery safety net).
        max_window_ms = int(max_seconds * 1000)
        if self.cumulative_audio_ms - start_ms > max_window_ms:
            start_ms = self.cumulative_audio_ms - max_window_ms

        # Translate ms range to byte offsets in the joined buffer.
        start_byte = int((start_ms - buffer_start_ms) * bytes_per_ms)
        start_byte = max(0, start_byte)
        # Align to even byte (PCM16 sample boundary).
        if start_byte % 2:
            start_byte -= 1

        return joined[start_byte:]


def _pcm16_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 LE mono in a minimal WAV/RIFF container so the
    transcribe service can ``soundfile.read()`` it without us shipping
    audio metadata in the request body.

    We do this in-memory (no temp file) because the buffers are small
    (~80 KB for 2.5 s of 16 kHz PCM16) and we want the call to be
    allocation-cheap on each flush.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # PCM16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


async def _flush_to_stt(
    state: _SessionState,
    websocket: WebSocket,
    *,
    is_final: bool,
    http_client: httpx.AsyncClient,
) -> None:
    """Build the current window into a WAV, POST to the streaming service,
    and emit a ``partial`` / ``final`` JSON frame back to the WS client.

    Concurrency: only one flush at a time per session (gated by
    ``state.in_flight``). If a flush is already in flight and the cadence
    fires again, we skip — the in-flight call already covers the window
    we'd be sending. Final flushes (on ``end``) override the gate so the
    last segment never gets dropped.
    """
    if not state.pcm_chunks:
        return

    if state.in_flight and not is_final:
        # Already a call in flight; skip this cadence tick.
        return

    state.in_flight = True
    state.flush_counter += 1
    seq = state.flush_counter
    pcm_at_call = state.take_pcm()
    cumulative_at_call = state.cumulative_audio_ms
    state.last_flush_at_audio_ms = cumulative_at_call

    # Phase B.3 polish: VAD silence gate. If the window is below the RMS
    # threshold, skip the parakeet call entirely — it would hallucinate
    # repeating tokens on quiet audio (the "a little bit of a little bit
    # of..." pattern). We still advance the consumed cursor so the next
    # flush starts fresh and silence doesn't accumulate as a backlog of
    # re-transcribe targets.
    if STREAM_VAD_ENABLED and not is_final and _is_silent(pcm_at_call):
        logger.info(
            "[streaming-vad] session=%s seq=%d skipping silent window "
            "(rms<%d, %d bytes, audio_ms=%d)",
            state.session_id, seq, STREAM_VAD_RMS_THRESHOLD,
            len(pcm_at_call), cumulative_at_call,
        )
        state.consumed_through_ms = cumulative_at_call
        state.in_flight = False
        return

    # Metric: every flush call counts as one audio-frame forward to the
    # upstream. Increment before the call so a hard crash mid-call still
    # leaves a trace in the counter.
    ws_audio_frames_forwarded_total.inc()
    started = time.time()

    try:
        wav_bytes = _pcm16_to_wav_bytes(pcm_at_call, state.sample_rate)

        # Phase B.3 chunk C: v2 endpoint serves per-word tokens_finalized +
        # tokens_draft instead of a single text + segments shape. Routes via
        # STREAMING_USE_V2_PARAKEET (flipped to 1 in v2.0.0 once the new
        # streaming-trained checkpoint shipped in v1.5.0). v1 endpoint stays
        # on the same parakeet-stream-svc image so rollback is a single env
        # flip.
        url = (
            f"{PARAKEET_STREAM_URL}/transcribe-stream-v2"
            if STREAMING_USE_V2_PARAKEET
            else f"{PARAKEET_STREAM_URL}/transcribe-stream"
        )
        headers = {
            "Content-Type": "audio/wav",
            "X-Session-Id": state.session_id,
            "X-Flush-Sequence": str(seq),
            "X-Is-Final": "1" if is_final else "0",
        }

        # Phase B.3 chunk D: fire sortformer in parallel with parakeet when
        # the flag is on. Sortformer is fast (~175 ms warm on RTX 3060) so
        # collecting its result after parakeet usually adds zero latency.
        # Failures here are non-blocking — we only annotate the partial JSON
        # with empty speakers, and the user gets the same transcript.
        sortformer_task: Optional[asyncio.Task] = None
        if STREAMING_USE_SORTFORMER and state.live_diarization_allowed:
            window_duration_ms = int(
                len(pcm_at_call) / 2 / state.sample_rate * 1000
            )
            window_start_ms = max(0, cumulative_at_call - window_duration_ms)
            sortformer_headers = {
                **headers,
                "X-Window-Start-Ms": str(window_start_ms),
            }
            sortformer_task = asyncio.create_task(
                http_client.post(
                    f"{SORTFORMER_URL}/diarize-stream",
                    content=wav_bytes,
                    headers=sortformer_headers,
                    timeout=STREAM_UPSTREAM_TIMEOUT_S,
                )
            )

        try:
            with parakeet_stream_request_duration_seconds.time():
                resp = await http_client.post(
                    url,
                    content=wav_bytes,
                    headers=headers,
                    timeout=STREAM_UPSTREAM_TIMEOUT_S,
                )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            logger.warning(
                "[B.2 WS] upstream_unreachable session=%s seq=%d err=%s",
                state.session_id, seq, exc,
            )
            # Cancel sortformer if it's still running — no point computing
            # speaker labels for a partial we won't emit.
            if sortformer_task is not None and not sortformer_task.done():
                sortformer_task.cancel()
            await websocket.send_json({
                "type": "error",
                "reason": "upstream_unreachable",
                "sequence": seq,
                "detail": str(exc)[:200],
                "retryable": True,
            })
            return

        if resp.status_code >= 400:
            logger.warning(
                "[B.2 WS] upstream_http_error session=%s seq=%d status=%d body=%s",
                state.session_id, seq, resp.status_code, resp.text[:200],
            )
            if sortformer_task is not None and not sortformer_task.done():
                sortformer_task.cancel()
            await websocket.send_json({
                "type": "error",
                "reason": "upstream_http_error",
                "sequence": seq,
                "status": resp.status_code,
                "detail": resp.text[:200],
            })
            return

        try:
            data = resp.json()
        except json.JSONDecodeError:
            if sortformer_task is not None and not sortformer_task.done():
                sortformer_task.cancel()
            await websocket.send_json({
                "type": "error",
                "reason": "upstream_bad_json",
                "sequence": seq,
                "detail": resp.text[:200],
            })
            return

        # Phase B.3 v2 endpoint: response shape is different.
        # v1: { text, segments, confidence, model, rtf }
        # v2: { tokens_finalized: [{word, start, end, confidence}, ...],
        #       tokens_draft, text_finalized, text_draft, model, rtf, ... }
        # Both shapes flow into ONE outbound JSON; clients consume whichever
        # set they understand. New fields ride alongside the legacy text/
        # segments so v1.4.x clients keep working.
        tokens_finalized: list = []
        tokens_draft: list = []
        text_draft: str = ""
        eou_detected: bool = False
        if STREAMING_USE_V2_PARAKEET:
            tokens_finalized = data.get("tokens_finalized") or []
            tokens_draft = data.get("tokens_draft") or []
            # Defensive: fall back to v1's `text` field when the v2 response
            # lacks text_finalized. Covers the edge case where backend env
            # says v2 but the svc actually returned v1 shape (e.g. mid-deploy
            # before the svc swap), or test fixtures using the v1 mock.
            text = (
                data.get("text_finalized")
                or data.get("text")
                or ""
            ).strip()
            text_draft = (data.get("text_draft") or "").strip()
            # EOU detection: the EOU token shows up as the last word's
            # text or as a suffix in text_finalized. Detect either way.
            if tokens_finalized:
                last_word = (tokens_finalized[-1].get("word") or "")
                if "<EOU>" in last_word or "<eou>" in last_word.lower():
                    eou_detected = True
            if not eou_detected and ("<EOU>" in text or "<eou>" in text.lower()):
                eou_detected = True
            # Strip the EOU sentinel from the user-facing text so the UI
            # does not render it as a word. We still surface it via the
            # eou_detected flag below.
            text = text.replace("<EOU>", "").replace("<eou>", "").strip()
            segments = []  # v2 omits chunk-level segments; per-word is the unit
            confidence = None  # v2 confidence lives per-token, not per-call
            model = data.get("model")
            rtf = data.get("rtf")
        else:
            text = (data.get("text") or "").strip()
            segments = data.get("segments") or []
            confidence = data.get("confidence")
            model = data.get("model")
            rtf = data.get("rtf")
            # v1 model (parakeet_realtime_eou_120m-v1) ALSO emits <EOU>
            # as a literal suffix in the text. Strip + surface as flag
            # so frontends can show a "stopped talking" indicator.
            if "<EOU>" in text or "<eou>" in text.lower():
                eou_detected = True
                text = text.replace("<EOU>", "").replace("<eou>", "").strip()

        # Phase B.3 chunk D: collect sortformer result if it was dispatched.
        # By this point parakeet has finished; sortformer is usually faster
        # (~175 ms vs parakeet's 200-600 ms) so the await is typically a
        # no-op. Failures are logged and silently dropped — sortformer is
        # an enhancement, not a blocking dependency.
        speakers: list = []
        sortformer_model: Optional[str] = None
        sortformer_rtf: Optional[float] = None
        sortformer_distinct_speakers: Optional[int] = None
        if sortformer_task is not None:
            try:
                sf_resp = await sortformer_task
                if sf_resp.status_code < 400:
                    sf_data = sf_resp.json()
                    raw_speakers = sf_data.get("speakers") or []
                    # Filter out turns shorter than SORTFORMER_MIN_SEGMENT_MS.
                    # Sortformer over-segments on solo / quiet audio (brief
                    # spectral diversity from breath sounds, fridge hum, etc).
                    # 500 ms floor catches real conversational turns and drops
                    # noise.
                    speakers = [
                        s for s in raw_speakers
                        if isinstance(s, dict)
                        and (s.get("end_ms", 0) - s.get("start_ms", 0))
                        >= SORTFORMER_MIN_SEGMENT_MS
                    ]
                    sortformer_model = sf_data.get("model")
                    sortformer_rtf = sf_data.get("rtf")
                    sortformer_distinct_speakers = sf_data.get(
                        "distinct_speaker_count"
                    )
                    # If the filter dropped every turn, also clear the
                    # distinct_speaker_count so the UI doesn't show stale info.
                    if not speakers:
                        sortformer_distinct_speakers = 0
                else:
                    logger.warning(
                        "[B.3 WS] sortformer_http_error session=%s seq=%d "
                        "status=%d body=%s",
                        state.session_id, seq, sf_resp.status_code,
                        sf_resp.text[:200],
                    )
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                json.JSONDecodeError,
                asyncio.CancelledError,
            ) as sf_exc:
                logger.warning(
                    "[B.3 WS] sortformer_unreachable session=%s seq=%d err=%s",
                    state.session_id, seq, sf_exc,
                )

        # De-dup empty / identical partials. Real model output won't usually
        # repeat verbatim across windows once audio actually arrives, but
        # zero-payload smoke tests produce empty text every time — flooding
        # the client with empty partials is noise.
        if not is_final and not text:
            # Empty text on real audio means parakeet declined to commit
            # tokens — advance the cursor anyway so the next flush gets
            # genuinely-new audio rather than the same silent-ish window.
            state.consumed_through_ms = cumulative_at_call
            return
        if not is_final and text == state.last_partial_text:
            state.consumed_through_ms = cumulative_at_call
            return
        state.last_partial_text = text

        # Phase B.3 polish: advance the consumed cursor on every successful
        # emission so subsequent flushes don't re-transcribe the same audio.
        # The lookback in ``take_pcm`` keeps a small overlap for word-boundary
        # continuity, so we don't lose words at the seam.
        state.consumed_through_ms = cumulative_at_call

        frame_type = "final" if is_final else "partial"
        await websocket.send_json({
            "type": frame_type,
            "sequence": seq,
            "text": text,
            "segments": segments,
            "confidence": confidence,
            "model": model,
            "rtf": rtf,
            "covers_through_ms": cumulative_at_call,
            "is_final": is_final,
            "upstream_elapsed_ms": int((time.time() - started) * 1000),
            # Phase B.3 chunk D: speaker labels from sortformer (empty when
            # STREAMING_USE_SORTFORMER is off or sortformer call failed).
            "speakers": speakers,
            "sortformer_model": sortformer_model,
            "sortformer_rtf": sortformer_rtf,
            "sortformer_distinct_speakers": sortformer_distinct_speakers,
            # Phase B.3 chunk C / v2.0.0 wire-up: per-word stream + EOU.
            # Empty arrays when STREAMING_USE_V2_PARAKEET is off or the
            # checkpoint doesn't expose word timestamps. eou_detected
            # also fires on v1 endpoint when the new EOU-trained model
            # (parakeet_realtime_eou_120m-v1) emits <EOU> in the text.
            "tokens_finalized": tokens_finalized,
            "tokens_draft": tokens_draft,
            "text_draft": text_draft,
            "eou_detected": eou_detected,
        })
        # Phase B.5 metric: count emitted partial/final back to client.
        ws_partial_transcripts_emitted_total.inc()

        logger.info(
            "[B.2 WS] flush ok session=%s seq=%d type=%s chars=%d "
            "audio_ms=%d elapsed_ms=%d",
            state.session_id, seq, frame_type, len(text),
            cumulative_at_call, int((time.time() - started) * 1000),
        )
    finally:
        # Phase B.5 slow-upstream check. If this call took longer than the
        # threshold, tell the next few cadence ticks to skip so the GPU
        # has room to catch up. Final flushes don't trigger the skip;
        # they're one-shot and we want the close path to complete cleanly.
        elapsed = time.time() - started
        if elapsed > STREAM_SLOW_THRESHOLD_S and not is_final:
            state.skip_next_n = max(state.skip_next_n, STREAM_SKIP_NEXT_ON_SLOW)
            logger.warning(
                "[streaming-backpressure] session=%s slow upstream "
                "elapsed=%.2fs threshold=%.2fs; skip_next_n=%d",
                state.session_id, elapsed, STREAM_SLOW_THRESHOLD_S,
                state.skip_next_n,
            )
        state.in_flight = False


@router.websocket("/ws/sessions/{session_id}/live")
async def session_live(websocket: WebSocket, session_id: str) -> None:
    """Phase B.2 WS endpoint with real forward path.

    Lifecycle:
      1. Auth-gate on oauth2-proxy headers, reject 4001 if missing.
      2. accept(), send ``ready`` frame with protocol version + upstream URL.
      3. Loop:
         - binary frame  -> parse 19-byte header, append PCM, maybe flush.
         - text {"type":"flush"} -> force flush.
         - text {"type":"end"}   -> final flush + close 1000.
         - text {"type":"ping"}  -> pong.
         - anything else         -> ack_control echo.
      4. On disconnect / exception: log + best-effort close.

    Upstream forwarding uses a single per-connection httpx.AsyncClient with
    HTTP keep-alive so we're not paying TCP setup on every flush.
    """
    # Auth: browser clients send the JWT as ?token= (the WebSocket API can't
    # set request headers); the oauth2-proxy path forwards identity headers.
    # Try the token first, then fall back to forward-auth. Both materialise
    # user._org_ids so the cross-tenant check + per-workspace gate work below.
    from auth.ws_auth import authenticate_ws
    user = await authenticate_ws(websocket)
    if user is None:
        user = _resolve_ws_user(websocket)
    if user is None:
        logger.warning(
            "[B.2 WS] unauthenticated connection attempt session_id=%s "
            "headers_present=%s has_token=%s",
            session_id,
            sorted(k for k in websocket.headers.keys() if k.startswith("x-")),
            bool(websocket.query_params.get("token")),
        )
        # Phase B.5 metric: log the rejection. tier=unknown because we
        # never resolved the user.
        ws_connections_total.labels(tier="unknown", result="auth_rejected").inc()
        ws_close_codes_total.labels(code="4001").inc()
        await websocket.close(code=4001)
        return

    user_email = user.email
    user_tier = get_user_tier(user)
    features = get_tier_features(user)

    # B.4 tier gate + billing-1 per-workspace gate. server_live must be in the
    # user's tier AND covered by the ACTIVE workspace's plan (the org that owns
    # this session). When the org resolves and the caller isn't a superuser the
    # org plan is authoritative; otherwise we fall back to the user tier alone
    # (mirrors gate_feature_for_caller with active_org=None). Accept the upgrade
    # first so we can send a clean JSON error frame, then close with 4003.
    active_org = _resolve_ws_session_org(session_id)
    org_allows = (
        True
        if active_org is None or getattr(user, "is_superuser", False)
        else org_covers_feature(active_org, "server_live")
    )
    if not (features.get("server_live") and org_allows):
        workspace_gap = bool(features.get("server_live")) and not org_allows
        logger.info(
            "[B.4 WS] tier_insufficient session_id=%s user=%s tier=%s "
            "workspace_gap=%s",
            session_id, user_email, user_tier, workspace_gap,
        )
        ws_connections_total.labels(tier=user_tier, result="tier_rejected").inc()
        ws_close_codes_total.labels(code="4003").inc()
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "reason": "tier_insufficient",
            "current_tier": user_tier,
            "required_feature": "server_live",
            "upgrade_hint": (
                "This workspace's plan doesn't include live streaming — "
                "upgrade the active organization."
                if workspace_gap
                else "A paid plan is required for live streaming."
            ),
        })
        await websocket.close(code=4003, reason="tier_insufficient")
        return

    # Phase B.5 per-org rate limit. Bucket the user by org id (or email
    # for orgless accounts) and reject with close 4429 if they already
    # have ``STREAMING_MAX_SESSIONS_PER_ORG`` live sessions open. We
    # check BEFORE accept so the rejected client gets a clean handshake-
    # reject + close, mirroring the unauth 4001 path. We also still
    # accept-and-send-JSON on the reject so clients that only listen on
    # onmessage see a structured error frame.
    org_bucket = _resolve_org_bucket(user)
    if _org_session_counts[org_bucket] >= STREAMING_MAX_SESSIONS_PER_ORG:
        logger.warning(
            "[streaming-rate-limit] rate_limited session_id=%s user=%s "
            "bucket=%s active=%d cap=%d",
            session_id, user_email, org_bucket,
            _org_session_counts[org_bucket], STREAMING_MAX_SESSIONS_PER_ORG,
        )
        ws_connections_total.labels(tier=user_tier, result="rate_limited").inc()
        ws_close_codes_total.labels(code="4429").inc()
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "reason": "rate_limited",
            "active_sessions": _org_session_counts[org_bucket],
            "max_sessions_per_org": STREAMING_MAX_SESSIONS_PER_ORG,
            "retryable": False,
        })
        await websocket.close(code=4429, reason="rate_limited")
        return

    from auth.ws_auth import ws_user_can_access_session
    if not ws_user_can_access_session(user, session_id):
        ws_close_codes_total.labels(code="1008").inc()
        await websocket.close(code=1008)
        return

    await websocket.accept()
    ws_connections_total.labels(tier=user_tier, result="accepted").inc()
    # Register this session for the SIGTERM drain hook + bump the
    # per-org count. Both are torn down in the finally block below.
    active_sessions[session_id] = websocket
    _org_session_counts[org_bucket] += 1

    logger.info(
        "[B.2 WS] connected session_id=%s user=%s tier=%s upstream=%s "
        "bucket=%s org_active=%d",
        session_id, user_email, user_tier, PARAKEET_STREAM_URL,
        org_bucket, _org_session_counts[org_bucket],
    )

    await websocket.send_json({
        "type": "ready",
        "session_id": session_id,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "protocol_version": PROTOCOL_VERSION,
        "phase": "B.2",
        "upstream": PARAKEET_STREAM_URL,
        "flush_audio_seconds": FLUSH_AUDIO_SECONDS,
        "tier": user_tier,
    })

    state = _SessionState(
        session_id=session_id,
        user_email=user_email,
        org_bucket=org_bucket,
        live_diarization_allowed=bool(features.get("live_diarization")),
    )
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)

    async with httpx.AsyncClient(limits=limits) as http_client:
        try:
            while True:
                message = await websocket.receive()

                msg_type = message.get("type")
                if msg_type == "websocket.disconnect":
                    code = message.get("code", 1006)
                    logger.info(
                        "[B.2 WS] disconnect frame session_id=%s code=%s",
                        session_id, code,
                    )
                    ws_close_codes_total.labels(code=str(code)).inc()
                    return

                if "bytes" in message and message["bytes"] is not None:
                    data: bytes = message["bytes"]
                    if len(data) < HEADER_SIZE:
                        logger.warning(
                            "[B.2 WS] frame_too_short session_id=%s got=%d expected>=%d",
                            session_id, len(data), HEADER_SIZE,
                        )
                        await websocket.send_json({
                            "type": "error",
                            "reason": "frame_too_short",
                            "got_bytes": len(data),
                            "expected_min_bytes": HEADER_SIZE,
                        })
                        continue

                    try:
                        seq, ts_us, fmt_bytes, sr100, flags = struct.unpack(
                            HEADER_STRUCT, data[:HEADER_SIZE]
                        )
                    except struct.error as exc:
                        logger.warning(
                            "[B.2 WS] header_unpack_failed session_id=%s err=%s",
                            session_id, exc,
                        )
                        await websocket.send_json({
                            "type": "error",
                            "reason": "header_unpack_failed",
                            "detail": str(exc),
                        })
                        continue

                    fmt_str = fmt_bytes.decode("ascii", errors="replace")
                    payload = data[HEADER_SIZE:]
                    payload_len = len(payload)
                    is_final_bit = bool(flags & 0x01)
                    sample_rate = sr100 * 100

                    state.last_seq = seq

                    # Only PC16 is appended to the buffer in B.2; other formats
                    # would need a transcode step we deliberately defer (Opus
                    # support is B.3 along with the AudioWorklet fallback).
                    if fmt_str == "PC16" and payload_len > 0:
                        state.append_pcm(payload, sample_rate)

                    await websocket.send_json({
                        "type": "ack",
                        "sequence": seq,
                        "received_bytes": len(data),
                        "header_bytes": HEADER_SIZE,
                        "payload_bytes": payload_len,
                        "format": fmt_str,
                        "sample_rate": sample_rate,
                        "is_final": is_final_bit,
                        "cumulative_audio_ms": state.cumulative_audio_ms,
                    })

                    # Cadence-driven flush: fire when we have enough audio.
                    # The header-level is_final bit also forces a flush;
                    # distinct from {"type":"end"} which closes the WS.
                    if state.should_flush() or is_final_bit:
                        asyncio.create_task(
                            _flush_to_stt(
                                state, websocket,
                                is_final=is_final_bit, http_client=http_client,
                            )
                        )
                    continue

                if "text" in message and message["text"] is not None:
                    raw_text = message["text"]
                    try:
                        ctrl = json.loads(raw_text)
                    except json.JSONDecodeError as exc:
                        logger.warning(
                            "[B.2 WS] bad_json session_id=%s err=%s preview=%r",
                            session_id, exc, raw_text[:80],
                        )
                        await websocket.send_json({
                            "type": "error",
                            "reason": "bad_json",
                            "detail": str(exc),
                        })
                        continue

                    ctrl_type = ctrl.get("type") if isinstance(ctrl, dict) else None

                    if ctrl_type == "end":
                        logger.info(
                            "[B.2 WS] client requested end session_id=%s audio_ms=%d",
                            session_id, state.cumulative_audio_ms,
                        )
                        await _flush_to_stt(
                            state, websocket, is_final=True, http_client=http_client,
                        )
                        await websocket.send_json({
                            "type": "closed",
                            "reason": "client_end",
                        })
                        ws_close_codes_total.labels(code="1000").inc()
                        await websocket.close(code=1000)
                        return

                    if ctrl_type == "flush":
                        logger.info(
                            "[B.2 WS] client requested flush session_id=%s "
                            "audio_ms=%d",
                            session_id, state.cumulative_audio_ms,
                        )
                        asyncio.create_task(
                            _flush_to_stt(
                                state, websocket,
                                is_final=False, http_client=http_client,
                            )
                        )
                        continue

                    if ctrl_type == "ping":
                        await websocket.send_json({"type": "pong"})
                        continue

                    logger.info(
                        "[B.2 WS] unknown_control session_id=%s ctrl=%s",
                        session_id, ctrl,
                    )
                    await websocket.send_json({
                        "type": "ack_control",
                        "echo": ctrl,
                    })

        except WebSocketDisconnect as exc:
            code = getattr(exc, "code", 1006)
            logger.info(
                "[B.2 WS] client disconnected session_id=%s code=%s",
                session_id, code,
            )
            ws_close_codes_total.labels(code=str(code)).inc()
        except Exception:
            logger.exception(
                "[B.2 WS] unexpected error session_id=%s user=%s",
                session_id, user_email,
            )
            ws_close_codes_total.labels(code="1011").inc()
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            # Phase B.5: drop the session from the active registry + bring
            # the per-org concurrent count back down. Both are guarded so
            # a partial shutdown (raced with the drain hook below) doesn't
            # crash us.
            active_sessions.pop(session_id, None)
            if _org_session_counts.get(org_bucket, 0) > 0:
                _org_session_counts[org_bucket] -= 1
                if _org_session_counts[org_bucket] <= 0:
                    _org_session_counts.pop(org_bucket, None)
