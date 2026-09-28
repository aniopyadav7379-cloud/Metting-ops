# Phase B: Server-Live Streaming for Paid Tier

Status: Draft for approval. Doc-only work; no code in this commit.
Implementation tickets at the end of this doc.
Owner: Meeting-Ops team.
Authoring date: 2026-05-22.

## 1. TL;DR

Phase B adds a second live path for users who can't run the browser stack
the desktop tier relies on. Mobile is the obvious case (iOS Safari WebGPU
is too slow, Android varies wildly, origin storage caps evict model
weights), but the same applies to underpowered desktops, Chromebooks,
locked-down work laptops, and anyone who installed Meeting-Ops yesterday
and doesn't want to wait through a 600 MB model download before their
first meeting. For those users, the recording itself still works (Phase
A capture-only ships chunks to `/audio-chunks` and the server reprocess
delivers transcript + summary about a minute after stop). What's missing
is the live experience: words on screen while the meeting is in
progress, a rolling summary that updates as the conversation moves.

Phase B fills that gap by streaming audio frames over a WebSocket to a
server-side pipeline (Parakeet streaming + a deferred/lightweight
diarization layer + Qwen 3.6 35B-A3B rolling summary on midboy1) and
pushing partial transcript and summary deltas back to the client. We
gate it to **Pro + Enterprise tiers**. Free mobile users continue on
capture-only. The compute-economics moat from `docs/compute-economics.md`
holds because we only burn server GPU time for users who pay us; free
users keep doing the work in their own browsers on desktop or get
capture-only on mobile. We never pay AssemblyAI / Deepgram per-minute
for anyone.

## 2. Why server-live for mobile and weak devices

The moat thesis lives in `docs/compute-economics.md`. Recap, with
Phase B's specific corollary.

Otter / Granola / Fathom / Fireflies / Read.ai pay a third-party API per
audio-minute and per token, for every user, for every meeting. AssemblyAI
streaming is roughly $0.37-0.65/hour depending on tier and diarization;
Deepgram Nova-3 streaming is roughly $0.46/hour with diarization. Live
LLM rollups on top of that add another $0.10-0.30 per meeting at OpenAI
or Anthropic rates. Their unit economics get worse the more users they
have.

We do the live work in the user's browser via Parakeet 0.6B INT8 +
Qwen 3 0.6B INT8 (or Gemma 4 E2B), cached in IndexedDB, run via
onnxruntime-web + transformers.js. Marginal cost per concurrent
user-hour is zero. Server compute is one 30-90 second pass at meeting
completion on our own GPUs that would otherwise be idle.

Mobile breaks the browser stack. Specifically:

- iOS Safari WebGPU is real but slow and unevenly supported. WASM
  fallback is real-time-borderline on a current iPhone, not viable on
  older devices.
- Android WebGPU is fragmented and SoC-dependent. Tensor G3 and
  Snapdragon 8 Gen 3+ are usable; most of the install base isn't.
- Origin storage on iOS Safari caps at a few hundred MB and evicts under
  pressure. Parakeet 0.6B INT8 + Qwen 3 0.6B together push that
  ceiling, and the next Safari restart can drop them on the floor.
- MediaRecorder on iOS suspends in background tabs, the screen
  going off, or app switching. Phase A's capture-only path tolerates
  this because it just stops capturing. A live path can't.
- Battery and thermals. Parakeet + Qwen running for 60 minutes in a
  phone browser is a real heater.

Phase A already handles these by setting honest expectations: mobile
gets capture-only, transcript + summary land at completion. That's the
shipped behavior in v0.8.0.

Phase B's job is to give *paying* mobile users the live experience too,
without poisoning the unit economics for everyone else. The framing:

> We pay compute only for users who pay us. Free users keep getting
> what the desktop browser gives them for free. Pro users get the live
> experience on any device, because they're financing the GPU minute.

Server-live is also the right answer for desktop browsers that can't
run the local stack:

- Old Intel Macs with no usable WebGPU.
- Locked-down corporate Chromebooks with WebGPU disabled at the policy
  layer.
- Anyone who hasn't pre-downloaded the model weights and starts a
  meeting in the next two minutes (cold-start would otherwise stall
  the live experience for 60-90 seconds while ~600 MB downloads).

For those cases Pro users get server-live as the fallback. Free desktop
users hit capture-only the same way mobile does.

## 3. Tier gating

### Entitlement model

The shipped backend already has org-scoped auth via Keycloak and
oauth2-proxy. It does not yet have a per-user subscription tier field
on the user model. We add one.

Proposed shape on `auth.models.User`:

```python
class User(Base):
    # ... existing fields ...
    tier: str = Column(String, default="free", nullable=False)
    # one of: "free" | "pro" | "enterprise"
    tier_source: str = Column(String, default="default", nullable=False)
    # one of: "default" | "manual" | "stripe" | "enterprise_seat"
    tier_updated_at: datetime = Column(DateTime, nullable=True)
```

`tier` is per-user, not per-org, because consumer Pro is a personal
purchase and enterprise seats are per-individual. Org-level enterprise
defaults can override per-user (an Enterprise org promotes all members
to at-least-Pro on join), but the field lives on the user.

`/api/auth/me` returns the tier today; today's response is the user +
their orgs. Add:

```json
{
  "email": "aaron@magicunicorn.tech",
  "tier": "pro",
  "tier_features": {
    "server_live": true,
    "server_reprocess": true,
    "brigade_graph": true,
    "speaker_library": true,
    "retention_days": null
  },
  "organizations": [ ... ]
}
```

The `tier_features` block is server-computed, not stored, so changes to
feature mapping don't need a DB migration. The frontend reads
`tier_features.server_live` and uses it as the entitlement gate, not
the raw tier string. This keeps the FE decoupled from tier-shape
changes.

### What each tier gets (live experience focus)

Pulling from `docs/compute-economics.md` and resolving where Phase B
fits.

| Capability | Free | Pro | Enterprise |
|---|---|---|---|
| Desktop browser live (Parakeet 0.6B + Qwen 3 0.6B) | yes | yes | yes |
| Mobile / weak-desktop capture-only | yes | yes | yes |
| **Server-live (Phase B)** | **no** | **yes** | **yes** |
| Server reprocess at completion | no | yes | yes |
| Speaker library + auto-match | no | yes | yes (org-wide) |
| Brigade graph push | no | yes | yes (BYOK) |
| Privacy mode (per-session) | yes | yes | yes (defaults on for HIPAA) |
| Retention controls | no | no | yes |

Free intentionally stays good. Desktop Free users get the same live
experience they have today. Mobile Free users get capture-only and the
transcript-at-completion path. Nothing breaks at the Free tier when
Phase B ships.

### UX surface for tier-locked features

When a Free user lands on the recorder on a device that can't do
browser-live (mobile, or desktop without WebGPU), they see capture-only
mode by default. The "Live transcript" pane is replaced by the existing
"Capture-only mode" banner shipped in v0.8.0, with one addition: a small
inline link below the banner reading "Want live captions on this device?"
that opens the upgrade screen.

The link is not a popup. It's not a modal. It's not a sales pitch. It's
one line of body-text-grey copy below the banner. The capture-only mode
itself works fully without ever clicking it.

Pro users on the same device see a server-live toggle in the recorder.
Toggling it on means "connect to the WebSocket and stream audio to the
server"; toggling it off means "stay on capture-only." Default for Pro
on mobile is **on**, because that's the value they're paying for.
Default on desktop without WebGPU is **on** for the same reason.

Enterprise behaves identically to Pro for the recorder UI. The
difference is in retention, BYOK Brigade graph, and HIPAA configuration,
none of which surface in the recorder.

### Privacy mode mutual exclusion

Privacy mode means "audio bytes never leave the device." Server-live
means "audio bytes stream to the server in real time." These are
mutually exclusive by definition.

UI rules:

- Toggling **privacy mode on** while server-live is active: warn,
  disconnect the WebSocket, fall back to whatever the device's local
  capability is (browser-live on desktop-capable, capture-only-local on
  mobile / desktop-fallback).
- Toggling **server-live on** while privacy mode is active: warn,
  disable privacy mode, connect the WebSocket. The session-level
  privacy lock from Phase A.5 still applies — toggling either mode
  mid-session is allowed for the *next* session, not the current one.
  Mid-session you get a "save and start a new session" prompt.
- On the recorder's mode-picker, Pro users see three live options:
  Server-live / Browser-live (when device-capable) / Privacy. Free
  users see Browser-live / Privacy when device-capable, or just
  Capture-only on mobile. Mode-picker copy mirrors the privacy / live
  tradeoff explicitly — "Server-live: fastest captions on any device,
  audio streams to our servers" vs "Privacy: audio never leaves your
  device, captions run locally when supported."

Pro users on mobile **can** still choose privacy mode if they want.
They won't get live captions (the local stack can't run reliably on
their device), but they get the post-stop local pass that A.6 added
when the device is capable enough to run it. On most current phones,
A.6 local pass at-stop on a 30-minute meeting takes 2-5 minutes; we
surface that latency explicitly in the privacy-mode option copy.

## 4. WebSocket protocol design

### Endpoint shape

```
wss://meetingops.magicunicorn.dev/ws/sessions/{session_id}/live
```

Hosted under the existing FastAPI app. Same Traefik / oauth2-proxy
front-end as the REST API.

Pre-conditions enforced on connect:

1. `session_id` exists in `recording_sessions` and is owned by an org
   the user is a member of.
2. Session `mode` is one of `always_on` or `personal`. Room sessions
   use their own pipeline (`websocket_remote_audio.py`).
3. Session `state` is in `recording` or `idle-ready` (sessions that
   have already stopped reject server-live; the data is gone).
4. User's `tier_features.server_live` is true. Else 403.
5. Privacy mode is not set on the session. Else 409.

### Auth handshake through oauth2-proxy

oauth2-proxy in front of Meeting-Ops checks the `_oauth2_proxy` cookie
on every request and forwards `X-Auth-Request-Email` /
`X-Auth-Request-Groups` headers to the upstream FastAPI. WebSockets
inherit the cookie because the upgrade request is a regular HTTP
request first; the cookie travels with it.

Known sharp edges, all manageable:

- **Cloudflare**. Cookies set on the apex `meetingops.magicunicorn.dev`
  by oauth2-proxy travel with the WS upgrade through CF because CF
  proxies the upgrade as a regular request. The dance is fine; no
  Worker required.
- **oauth2-proxy WebSocket tunnel mode**. Once oauth2-proxy upgrades
  the connection to a tunnel, subsequent frames go straight to upstream
  without re-checking auth. We rely on this. If the upstream session
  outlives the cookie's validity (long meetings + short cookie TTL),
  the server still owns the open socket but the user's REST calls
  start failing. Our cookie TTL is 30d idle / 1y max
  (`reference_keycloak_topology.md`), so this is fine in practice.
- **WS keepalive vs Cloudflare 100s idle timeout**. CF closes
  WebSockets that haven't sent a frame for 100 seconds. Our backend
  emits a `{"type":"keepalive"}` frame every 30 seconds. The client
  has its own keepalive at 30s.
- **Cookies on subdomain federation**. If we ever serve from a
  different subdomain than where oauth2-proxy sets cookies, the
  upgrade fails. We don't today, but flag this for the C-2 native iOS
  / watchOS case where the native app does its own OIDC and doesn't
  have an oauth2-proxy cookie. Native clients use a Bearer token in
  `Sec-WebSocket-Protocol: bearer.<JWT>` — see Section 12.

### Message framing

JSON for control + text frames. Binary for audio frames. Multiplexed
on the same socket; FastAPI's `WebSocket` distinguishes via
`receive_text()` vs `receive_bytes()`.

#### Client → server

**Audio frame (binary):**

Fixed 19-byte header (big-endian, struct format `>IQ4sHB`), followed
by the raw audio payload:

```
bytes 0-3    uint32 BE  sequence_number       (per-stream monotonic)
bytes 4-11   uint64 BE  client_timestamp_us   (microseconds, NTP-like)
bytes 12-15  4-char ASCII  payload_format
               "PC16" = 16 kHz mono signed-LE PCM16
               "OPUS" = OggOpus
               "AACL" = AAC-LC
bytes 16-17  uint16 BE  sample_rate / 100     (160 = 16000 Hz)
byte 18      uint8       flags
               bit 0: is_final (last frame in this segment)
bytes 19..N  audio payload
```

Sequence number increments per frame. The client picks the format
based on what its MediaRecorder produces. Browsers settle on
`audio/webm;codecs=opus` (Chrome/FF), `audio/mp4;codecs=mp4a.40.2`
(Safari/WebKit). For the WebSocket path we transcode in-browser via
AudioWorklet to 16kHz mono pcm16, eliminating server-side decoding
entirely. PCM16 at 16kHz mono is 32 kB/sec, which is well within any
reasonable mobile uplink. We can fall back to Opus if AudioWorklet
isn't available, but PCM is simpler on the server.

**Framing decision (B.2 reconciliation, 2026-05-22):** the original
draft of this doc specified a variable-length little-endian header
with a leading version byte (`0x01`, then LE uint64 sequence, LE
uint64 timestamp_ms, LE uint16 format, payload). Phase B.1 shipped a
fixed 19-byte big-endian header that's smaller (19B vs 19B + version
overhead in the variable layout), cheaper to parse (`struct.unpack`
with a fixed format string vs read-version-then-branch), and trivially
struct-pack/unpack-compatible from both Python and TypeScript. Both
layouts carry the same information; the fixed BE form is simpler to
implement correctly. B.2 standardised on the fixed BE form across
`backend/api/streaming.py`, `services/parakeet-stream-svc/main.py`,
and `frontend/src/pages/StreamingTest.tsx`. The variable LE form
remains a future option if we ever need extension fields, but until
then it's over-engineered for v1.

**Control frame (text JSON):**

```json
{"type": "hello", "session_id": "...", "client_version": "0.9.0",
 "device_capability": "capture-only" | "desktop-capable" | "desktop-fallback",
 "preferred_summary_cadence_sec": 5}
{"type": "ping", "ts_ms": 1234567890}
{"type": "stop"}
{"type": "pause"}
{"type": "resume"}
{"type": "audio_meta", "sample_rate": 16000, "channels": 1, "format": "pcm16"}
```

#### Server → client

**Control frames (text JSON):**

```json
{"type": "ready", "transcribe_provider": "parakeet-1.1b-streaming",
 "summary_model": "qwen-3.6-35b-a3b", "diarize": "deferred"}
{"type": "transcript_partial",
 "session_id": "...",
 "from_ms": 12340, "to_ms": 14820,
 "sequence": 47,
 "text": "and so the budget for Q3 is going to need",
 "stable_through_ms": 12820,
 "speaker": "spk_0" | null}
{"type": "transcript_final",
 "session_id": "...",
 "from_ms": 12340, "to_ms": 14820,
 "sequence": 47,
 "text": "And so the budget for Q3 is going to need",
 "speaker": "spk_0" | null,
 "confidence": 0.94}
{"type": "summary_delta",
 "session_id": "...",
 "delta_index": 3,
 "covers_through_ms": 180000,
 "summary_md": "## Where we are\n- ...\n\n## Action items\n- ..."}
{"type": "keepalive", "ts_ms": 1234567890}
{"type": "pong", "client_ts_ms": 1234567890, "server_ts_ms": 1234568010}
{"type": "error",
 "session_id": "...",
 "code": "backpressure" | "gpu_saturated" | "auth_expired" |
         "invalid_state" | "internal",
 "message": "...",
 "retryable": true,
 "retry_after_ms": 2000}
{"type": "tier_revoked", "reason": "..."}
```

Notes on the shape:

- `transcript_partial` is "what Parakeet thinks you said so far,
  including unstable trailing tokens." `stable_through_ms` is the
  point before which the text won't change.
- `transcript_final` is "this chunk is locked." Triggered when a
  silence boundary or speaker-turn boundary lands. The frontend
  replaces partials in the same `[from_ms, to_ms]` range with the
  final.
- `summary_delta` is sent on a server-controlled cadence (default
  every 5s, client can request 3-10s via hello). Body is a full
  rendered Markdown summary up through `covers_through_ms` ms of audio.
  We do not send diffs; the summary is small enough that re-sending
  the full Markdown is cheaper than maintaining a diff protocol.

### Sequence numbers, replay, reconnection

Client maintains its own monotonic `sequence_number` for outbound
audio frames. Server tracks the highest received sequence per
`session_id`. On reconnect the client sends `hello` with the highest
sequence it has ACK'd; server responds with `ready` + the highest it
has received, and the client replays any audio frames in between.
Frames older than 30 seconds at reconnect time are dropped (the live
window has passed; the server-reprocess pipeline at session end will
catch them in the chunked upload, which runs in parallel — see
Section 5).

The client also keeps a *separate* outbound queue feeding the regular
`/audio-chunks` REST endpoint. Server-live is additive; the chunked
upload still runs so the server reprocess at completion produces the
high-quality transcript + summary even if the live socket has been
flaky. The live socket exists to push partials to the user *now*; the
chunks pipeline exists to give the user the final artifact.

Reconnect cadence: exponential backoff 1s / 2s / 4s / 8s / 16s capped
at 30s. After three failed reconnects, the client falls back to
capture-only behavior (still uploads chunks, still gets transcript at
completion, just no live captions) and surfaces a banner: "Live
captions disconnected. Audio is still being saved." The fallback is
not silent.

### Backpressure

The server publishes a `slot_status` field on `ready` and updates it
on `error.code=backpressure`:

```json
{"type": "slot_status",
 "available_slots": 2,
 "queue_depth_ms": 3500}
```

If `queue_depth_ms > 5000`, the client should reduce summary cadence
from 5s → 10s and drop the summary panel update rate. The client
chooses; the server only informs.

If the server gets fully saturated (all Parakeet slots full + queue
backed up), it sends `error.code=gpu_saturated` with `retryable=true,
retry_after_ms=5000`. The client disconnects, waits, reconnects.
During that window the user falls back to capture-only with a visible
banner.

## 5. Backend design

### New FastAPI WS endpoint

`backend/api/websocket_live_server_stream.py`. New module to keep the
Phase B surface isolated from existing WS endpoints (auto_summary,
remote_audio, satellite, transcription). Each of those serves a
specific surface and rewriting one of them to do double-duty would
make all of them harder to reason about.

Sketch:

```python
@router.websocket("/ws/sessions/{session_id}/live")
async def websocket_server_live(
    websocket: WebSocket,
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_from_ws),
):
    if not user.tier_features.get("server_live"):
        await websocket.close(code=4403, reason="tier")
        return
    session = db.query(RecordingSession).filter(
        RecordingSession.id == session_id,
        RecordingSession.org_id == user.active_org_id,
    ).first()
    if not session:
        await websocket.close(code=4404, reason="not_found")
        return
    if session.state not in ("recording", "idle-ready"):
        await websocket.close(code=4409, reason="invalid_state")
        return
    if session.privacy_mode:
        await websocket.close(code=4409, reason="privacy_mode")
        return

    async with LiveStreamSession(session_id, user.id) as live:
        await live.run(websocket)
```

`LiveStreamSession` is the per-session orchestrator: it owns the
audio ring buffer, the Parakeet streaming client, the summary
scheduler, and the outbound message channel. One asyncio task per
session, plus inference calls fan out to the shared model services.

### Audio ring buffer

Per session, in-memory. The ring holds the last 60 seconds of PCM16 +
a tail of unstable text from Parakeet. Format:

```python
@dataclass
class AudioRing:
    pcm: collections.deque  # 60s of PCM16 frames
    received_ms: int  # cumulative audio received
    stable_text_through_ms: int  # last final-locked boundary
    transcript_partials: list[TranscriptSpan]  # text since stable boundary
    transcript_finals: list[TranscriptSpan]  # locked spans for the rolling window
```

We do NOT persist the ring. The full transcript comes from the
finalize-audio pipeline at session end. The ring is only for live
delivery. If the session crashes mid-meeting, the chunks endpoint
still has the bytes; reprocess produces the canonical transcript.

### Parakeet streaming integration

The shipped `parakeet-server` on midboy2 (GPU 0) and bigboy hosts
NVIDIA Parakeet TDT 1.1B fp16, configured for whole-file batch
transcription. Phase B needs streaming.

Two paths to investigate (open question, see Section 12):

**Option A: streaming via fixed-size sliding windows.** Today's
parakeet-server takes a WAV path or buffer and returns the full
transcript. We can simulate streaming by feeding it 1.0-1.5 second
sliding windows with 200ms overlap and stitching the results on the
client. Per-window inference on a 3060 is ~80-150ms for Parakeet
1.1B fp16 (extrapolating from the offline RTFx, batched 1). Total
end-to-end latency: 1.0-1.5s window + 150ms inference + 50-100ms WS
round-trip = ~1.5-2s. Acceptable. The downside is repeated work and
worse word-level alignment; we'd want to ship the smaller
**Parakeet 0.6B v2 / v3** for the live path which is far cheaper per
window and degrades quality only a little vs 1.1B.

**Option B: native streaming via finalized+draft tokens.** The
Parakeet TDT family supports streaming via configurable left+right
context windows (per the NVIDIA Modal example and the recent
NeMo streaming API). This is the path Modal's example uses; it
produces finalized tokens + draft tokens in a streaming protocol.
Lower latency (~250-400ms first-word), no repeated work. Requires
either:

- Standing up a second container on midboy2 GPU 0 that runs
  Parakeet 0.6B v2 in NeMo's streaming mode (probably the right
  call: it's a different model size with a different config), or
- Migrating parakeet-server to expose a streaming endpoint
  alongside the existing batch endpoint.

We default to **Option B with a Parakeet 0.6B v3 streaming service**.
0.6B v3 is multilingual, smaller, and 2026-vintage. It runs on the
3060 with room to spare alongside the existing 1.1B fp16 batch
container. We add a new service `meet-parakeet-stream-svc` on midboy2
GPU 0 listening on :8881-stream (or a sibling port) exposing a
WebSocket. The Meeting-Ops WS endpoint relays frames to it and
proxies finalized/draft tokens back. Section 12 flags the parakeet-
server streaming-mode survey as an open question (we need to verify
whether the current container's NeMo build supports streaming mode
without a rebuild).

If Option B doesn't pan out in implementation, Option A (sliding
windows on the existing parakeet-server) is a forward-compatible
fallback. The wire contract above is the same either way.

### Diarization on live: defer, light, or off

pyannote 3.1 is batch-only. It runs over the whole audio to do
agglomerative clustering. There is no live mode that produces the
same quality. The shipped server-reprocess pipeline runs pyannote at
session end and that's where speaker-name attribution comes from.

For Phase B's live path we have three choices, in increasing scope:

**Choice 1 (default): defer diarization entirely.** Live transcript
ships with no speaker labels. Bubbles in the UI show generic "Speaker
talking..." or just unattributed text. At session end the
finalize-audio reprocess produces the canonical transcript *with*
speaker names. The user sees them backfill in the SessionDetails view
about a minute after stop.

**Choice 2 (medium scope): light speaker-turn detection.** Run a
small VAD-aware speaker-turn detector (e.g., pyannote-segmentation in
streaming mode, or a small custom turn-change classifier) to bubble
the transcript by speaker turn even without speaker identity. Labels
are "Speaker 1 / Speaker 2 / ..." but stable within the session. At
session end the canonical speaker names backfill the same way.

**Choice 3 (full scope): Sortformer streaming.** NVIDIA's
`nvidia/diar_streaming_sortformer_4spk-v2` exists, 2025/2026 vintage,
0.32s chunk latency, DER notably better than pyannote on standard
benchmarks, but capped at 4 speakers. Runs on midboy2 GPU 0 alongside
Parakeet streaming (~1-1.5GB additional VRAM, within budget). At
session end pyannote 3.1 still runs as the canonical diarizer (it
handles arbitrary speaker counts and is what our speaker library is
trained against), but live uses Sortformer's stable, fast speaker
indices.

We default to **Choice 1 for Phase B.1 (initial ship)** with a
shipped plan to upgrade to Choice 3 in Phase B.2 or B.3. The 4-speaker
limit of Sortformer is fine for 90% of meetings; the canonical
pyannote pass handles the >4 case correctly at session end. The
choice is reversible: a Choice-3 live attribution that doesn't match
the canonical pyannote attribution simply gets renamed at session end,
same as Choice 1 backfilling labels.

### LLM rolling summary

Qwen 3.6 35B-A3B-Vision on midboy1 P40 (via LiteLLM
`Qwen3.6-35B-A3B-Vision` alias) handles all Meeting-Ops LLM work
today via `triggered_by='auto-words'` slice rollups from the existing
`services/summary_slices.py`. Phase B reuses this path.

Cadence: every N seconds OR every M words, whichever comes first.
Default: **5 seconds OR 200 words.** Server-controlled; client can
request a slower cadence via hello (`preferred_summary_cadence_sec`)
on slow networks or low-power devices.

Prompt: same Markdown summary template as the existing `triggered_by=
'auto-words'` slice rollup, with two modifications:

- Pass only the *new* transcript since the last summary delta + the
  *previous* summary as Markdown. Qwen 3.6 35B-A3B holds previous
  summary context cheaply (no-think mode keeps token budget tight).
- Ask for the same shape every time: `## Where we are` / `## Action
  items` / `## Decisions` / `## Open questions` so the client can
  re-render predictably.

Per-call cost: ~1-3K input tokens (previous summary + last 5s of
transcript), ~500-1500 output tokens (full re-rendered summary). On
midboy1's P40 at `np=4 -c 98304`, that's ~1-3s wall-clock. With 5s
cadence, we're inside the budget.

Concurrency: 4 concurrent Pro users sharing one P40 at np=4
continuous batching. Beyond 4, we either queue summary deltas (the
client sees a longer cadence; not the end of the world) or stand up
the second P40 via a parallel LiteLLM upstream (already wired,
benchmarked, documented in `feedback_dual_p40_qwen.md`).

### Concurrency model

Per session:

```
[ FastAPI WS task ]
   ├── audio ingress loop (receive binary frames, push to ring)
   ├── parakeet streaming client (push partials/finals to outbound)
   ├── summary scheduler (every 5s OR 200 words, fire-and-await Qwen call)
   ├── outbound message queue (single asyncio.Queue back to client)
   └── keepalive ticker (30s)
```

Single uvicorn worker handles many sessions concurrently via asyncio.
The expensive work happens on Parakeet + Qwen services over HTTP, not
in the FastAPI worker. Worker CPU is for routing and serialization,
which is cheap.

Multi-worker scaling: the WS handler is stateless across sessions
(each session's ring lives only in its handler task), so we don't
*need* Redis pub/sub for cross-worker coordination. We need only
sticky routing — Traefik can do this via the `consistent` LB strategy
keyed on the session_id path segment. Open question in Section 12:
do we want Redis pub/sub anyway for observability + future multi-
device-per-session use cases (e.g., a desktop dashboard view of a
mobile-recorded session). Default: defer.

### Server reprocess at session end (unchanged)

Phase B does not change the server-reprocess pipeline. When the user
stops, the existing `/finalize-audio` path runs Parakeet 1.1B fp16 +
pyannote + Qwen 3.6 final summary against the full audio. The live
transcript is replaced by the canonical transcript; the live summary
is replaced by the final summary. The user sees this as the existing
"Server reprocess in progress" banner.

The chunks pipeline runs in parallel with the WebSocket. The WS
gives the user *now*. The chunks give the user the *final artifact*.
They are independent.

## 6. Frontend design

### WebSocket client module

New service: `frontend/src/services/liveStreamClient.ts`. Encapsulates
the wire protocol from Section 4. Exposes:

```ts
interface LiveStreamClient {
  connect(sessionId: string): Promise<void>;
  disconnect(): void;
  on(event: "transcript_partial" | "transcript_final" | "summary_delta"
       | "error" | "status", cb: (e: any) => void): void;
  sendAudio(pcm: Int16Array, ts: number): void;
  sendControl(msg: ControlMessage): void;
  readonly state: "idle" | "connecting" | "connected" | "reconnecting"
                  | "fallback";
}
```

Connection lifecycle:

1. `connect()` opens the WS at `wss://meetingops.../ws/sessions/{id}/live`.
2. On open, send `hello`. Wait for `ready`. Mark `connected`.
3. Audio frames are produced by an AudioWorklet on the same MediaStream
   the recorder uses. The worklet pulls 100ms PCM16 chunks, the client
   batches them into 200ms frames (to halve frame rate), assigns
   sequence numbers, and sends as binary.
4. Inbound `transcript_partial` / `transcript_final` events bubble to
   the subscribed handler.
5. `summary_delta` events bubble to the subscribed handler.
6. On WS close / error: exponential backoff reconnect. Send hello with
   the last-ACK'd sequence. Replay buffered audio frames since then.
7. After 3 failed reconnects, transition to `fallback` and stop trying.
   The chunks pipeline keeps running; the user just won't get live
   captions.

Browser-side audio transcoding: AudioWorklet → 16kHz mono PCM16. The
worklet is a separate file `frontend/public/audio-worklets/pcm-encoder.js`
(must be served as a separate URL, not inlined). Falls back to
ScriptProcessorNode on browsers without AudioWorklet (Safari < 14.1;
unlikely to matter in 2026).

### Integration in AlwaysOnContext

`AlwaysOnContext.tsx` is the orchestration brain (2507 lines, owns
the recorder, the VAD engine, the IDB persistence, the privacy mode
lock, the orphan-session resume, the slice triggering). Phase B adds
a server-live branch in the same provider.

New context state:

```ts
serverLiveAvailable: boolean;
serverLiveEnabled: boolean;
serverLiveState: "idle" | "connecting" | "connected" | "reconnecting"
                | "fallback";
serverLiveTranscript: TranscriptSpan[];  // partials + finals merged
serverLiveSummary: string | null;
```

New context methods:

```ts
enableServerLive(): Promise<void>;
disableServerLive(): void;
```

`serverLiveAvailable` is true when:

- `auth.me.tier_features.server_live === true`
- Session is not in privacy mode
- Browser supports WebSocket + AudioWorklet (or ScriptProcessor
  fallback)

`serverLiveEnabled` is the user's choice. Defaults: `on` if
`deviceCapability !== 'desktop-capable'` (i.e., mobile or
desktop-fallback) AND `serverLiveAvailable`. Otherwise `off` (desktop-
capable users use the browser stack; they can opt in to server-live
if they want, e.g., debugging).

On `start()`:

```
if (serverLiveAvailable && serverLiveEnabled) {
  await liveStreamClient.connect(sessionId);
  // browser inference loops skipped on capture-only devices
  // already (Phase A); on desktop-capable they're optional, default off
  //   when server-live is on, so we don't double-pay
}
```

On `stop()`:

```
liveStreamClient.sendControl({type: "stop"});
liveStreamClient.disconnect();
// existing finalize-audio pipeline runs as usual
```

### UI components

- **`AlwaysOnControl.tsx`**: existing recording panel. Add a "Live mode"
  pill in the top-right showing one of: `Browser-live` / `Server-live` /
  `Capture-only` / `Privacy`. Click opens the mode picker.
- **Mode picker modal**: 3-4 options depending on tier/device. Free
  desktop-capable: Browser-live / Privacy. Free mobile / weak desktop:
  Capture-only / Privacy. Pro any device: Server-live / Browser-live
  (when capable) / Privacy. Each option has 1-line copy explaining
  the privacy/quality/latency tradeoff. Selection is per-session and
  locks at start.
- **`AlwaysOnControl` live panes**: when `serverLiveState=connected`,
  replace the existing browser-Parakeet pane with the
  `serverLiveTranscript` view. Same scrollback, same speaker bubbles
  (with placeholder "Speaker talking..." when diarization is deferred),
  same auto-scroll-to-bottom behavior. When `serverLiveState=
  reconnecting`, show a small amber "Reconnecting..." banner above the
  pane. When `serverLiveState=fallback`, show "Live captions
  disconnected. Audio is still being saved. You'll get the full
  transcript when your meeting ends." Same banner copy mobile and
  desktop.
- **`MobileLiveRecording.tsx`**: similar substitution. The capture-only
  banner that shipped in v0.8.0 is replaced with the live transcript
  pane when `serverLiveState=connected`. Same fallback messaging when
  the WS drops.
- **Upgrade nudge for Free mobile users**: below the capture-only
  banner, one line: "Want live captions on this device? Upgrade to
  Pro." Link to the upgrade page. No popup, no modal.

### Tier-revoked handling

If a Pro user's subscription lapses mid-session, server sends
`tier_revoked`. The client disconnects the WS, transitions to fallback,
shows: "Live captions ended (subscription expired). Audio is still
being saved." The chunks pipeline continues. The user gets their
transcript at completion. We do not silently downgrade.

### Telemetry (lightweight)

The frontend emits client-side latency metrics to a single endpoint
`POST /api/recordings/sessions/{id}/live-metrics` on session stop:

```json
{
  "first_word_latency_ms": 480,
  "p50_partial_latency_ms": 620,
  "p95_partial_latency_ms": 1100,
  "summary_deltas_count": 12,
  "p50_summary_latency_ms": 1400,
  "reconnects": 0,
  "fallback_triggered": false,
  "ws_total_bytes": 1845000
}
```

Server logs to Prometheus via existing observability path. We use this
to validate the latency budget (Section 8) in production and tune
cadence.

## 7. Cost modeling

### Our cost per concurrent Pro user-hour

| Component | Cost |
|---|---|
| Parakeet 0.6B v3 streaming on midboy2 3060 (1/8 of GPU per user) | ~0 (idle GPU) |
| Qwen 3.6 35B-A3B summary on midboy1 P40 (1/4 of GPU per user) | ~0 (idle GPU) |
| Bandwidth (PCM16 16kHz mono, 32KB/sec, ~115MB/hour upstream + ~5MB/hour transcript+summary downstream) | <$0.01 |
| FastAPI worker CPU (per-session asyncio task) | trivial |

Aaron's GPU hardware (already documented in `project_aaron_hardware.md`):
midboy1 has 2× P40, midboy2 has 2× RTX 3060, bigboy has 3090 + Quadro
RTX 6000. Right now these run inference jobs but mostly sit idle. We
already paid the capital cost; the marginal cost of running Phase B
on them is essentially the power bill.

Concurrent capacity (rough):

- **Parakeet 0.6B v3 streaming on midboy2 GPU 0**: shared with the
  current 1.1B fp16 batch service. The 1.1B uses ~3.5GB VRAM and runs
  bursty (one job per session-finalize), so we can co-host a 0.6B v3
  streaming service (~2GB VRAM) on the same GPU with headroom. The
  streaming service handles ~20-30 concurrent sessions on a single
  3060 at the latency budget below. If we saturate, midboy2 GPU 1 is
  available (currently runs Infinity + Kokoro at ~7.5GB used, ~4.5GB
  free; not ideal for parakeet but possible at low concurrency); or
  we stand up a second instance on bigboy 3090 (massive headroom).
- **Qwen 3.6 35B-A3B on midboy1 P40**: 4 concurrent users at np=4
  continuous batching today. Stand up the second P40 (already wired,
  documented in `project_dual_p40_qwen_results.md`) for 8 concurrent.
  Beyond 8, we either accept slower summary cadence (every 10s, still
  fine) or add midboy1 GPU peer + bigboy 3090 to the LiteLLM upstream
  pool.

Conservative ceiling on existing hardware: **~20 concurrent Pro users
in active meetings, with comfortable headroom and no quality
degradation.** That's enough for product-market-fit phase. Past 50-100
concurrent we'd want to add inference capacity, but past 50 concurrent
we'd also have Pro revenue funding it.

### Competitor cost (the gap that funds Pro)

From `docs/compute-economics.md`, validated against May 2026 pricing:

| Vendor | Streaming STT | Diarization add-on | Live LLM | Per hour-user |
|---|---|---|---|---|
| AssemblyAI Universal-3 Pro streaming | $0.37/hr | included tier | OpenAI tokens | ~$0.50-0.70 |
| Deepgram Nova-3 streaming | $0.46/hr | included | OpenAI tokens | ~$0.60-0.80 |
| Self-hosted Whisper-large-v3-turbo on cloud GPU | ~$0.30/hr | ~$0.10 | ~$0.15 | ~$0.55 |

So competitors burn **$0.50-0.80 per concurrent Pro user-hour**. At
1000 concurrent users, that's $500-800/hour of pure unit cost. At our
hardware-amortized rate it's **~$0.01/hour all-in**.

### Pricing implication

At Pro = $15/mo (illustrative — pricing decisions still pending), one
user generating ~5 meeting-hours per week is ~22 meeting-hours per
month. Their unit cost to a competitor: ~$11-18. To us: ~$0.20. Our
gross margin on a $15 Pro plan with that usage: 98%. Otter / Granola
on a $15 plan with the same usage: 25-100% margin compression depending
on exact tier.

At Pro = $25/mo, $5/mo of the price covers compute generously; the rest
funds platform, product, and the rest of the team. The Phase B add-on
specifically makes the unit economics structurally better than the
competition for any user on any device.

### What we don't subsidize

Free users on mobile get capture-only and a server reprocess at
completion. Server reprocess is one 30-90 second GPU pass per meeting;
on a 3060 + a P40, marginal cost is **~$0.005-0.02 per completed
meeting** at retail GPU rates we don't pay. We can keep Free's
completion pipeline running indefinitely. Server-live is what we
charge for.

The cleanly drawn line is: **the per-minute concurrent work** is what
generates ongoing GPU load. We charge for it. The **one-pass completion
work** is bounded per meeting regardless of meeting length. We give
that away.

## 8. Latency budget

### Targets

| Metric | Target | Hard cap |
|---|---|---|
| First-word latency (user speaks → text appears) | <500ms | <1500ms |
| p50 partial latency | <700ms | <1200ms |
| p95 partial latency | <1500ms | <3000ms |
| Summary delta cadence | 5s | 10s |
| Summary delta wall-clock | <2s | <5s |
| Reconnect time | <2s for 1st attempt | <30s total |

### Where the ~500ms first-word latency comes from

```
[ client mic → AudioWorklet 100ms frame ]    100ms
[ batch to 200ms WS frame ]                   100ms (buffered)
[ network upstream to backend ]              30-80ms
[ ring buffer push + parakeet stream call ]  10-30ms
[ Parakeet 0.6B v3 streaming inference ]    200-400ms
[ partial JSON back over WS ]                30-80ms
[ frontend render ]                          10-20ms
                                           ────────────
                                           ~480-810ms
```

Acceptable for "subjectively live." Otter and Granola hit roughly
500-1500ms first-word; AssemblyAI streaming hits ~300-500ms (their
infra is purpose-built and they charge for it). We're in the same
class as the paid competition.

### Where summary deltas land

```
[ 5s of new audio accumulates ]              5000ms
[ Parakeet finalizes the trailing edge ]    ~500ms (overlap with cadence)
[ Qwen 3.6 35B-A3B summary call np=4 ]      1000-3000ms
[ summary JSON back over WS ]                30-80ms
[ frontend re-render ]                       10-20ms
                                           ────────────
First summary delta visible to user:
   5s cadence window + ~1.5s rollup = ~6.5s wall-clock
```

p50 summary latency target of 1.4s is the *inference + transport*
portion only (not the 5s cadence window). The user perceives a fresh
summary every 5-7s, which matches Otter/Granola.

### Where we trade quality for latency

- **Diarization deferred** (Section 5, Choice 1): no live speaker
  labels. The trade is "Pro users sometimes see uncolored bubbles
  for the first minute of a session" in exchange for ~500ms saved on
  every partial. Acceptable. Sortformer (Choice 3) lands in Phase
  B.2/B.3 and removes the trade.
- **Parakeet 0.6B v3 vs 1.1B**: live transcript is slightly less
  accurate on rare-vocab. The canonical 1.1B fp16 transcript backfills
  at session end. The user sees the upgrade in real time on the
  SessionDetails page after stop.
- **Summary every 5s, not every word**: the rolling summary is "what
  has happened so far," not "what was just said." Word-level rolling
  is unnecessary and would cost 10x compute. 5s is the right cadence.

### Where we cannot trade quality for latency

- **Final transcript at session end**: stays canonical. Server reprocess
  is unchanged.
- **Final summary at session end**: stays canonical. Same Qwen 3.6
  35B-A3B-Vision pass we run today.
- **Action item extraction**: lives in the canonical pipeline only.
  Phase B's live summary mentions action items inline ("## Action
  items: ..."), but the structured `action_items` table is populated
  exclusively from the canonical post-stop pass. We don't ship
  half-extracted action items to the action-items board mid-meeting.

## 9. Failure modes and graceful degradation

### WebSocket connection drops mid-session

Client tries 3 exponential-backoff reconnects (1s/2s/4s/8s/16s, cap
30s). Each reconnect replays buffered audio since the last ACK'd
sequence. After 3 failures, transitions to `fallback`. The chunks
pipeline continues uninterrupted; the user gets the canonical
transcript at session end. A persistent amber banner reads "Live
captions disconnected. Audio is still being saved."

### Server overloaded (all Parakeet slots full)

Server sends `error.code=gpu_saturated` with `retry_after_ms=5000` on
the WS upgrade or shortly after. Client disconnects and queues a retry.
During the retry window the user sees "Server busy. Trying again in
5s." If the retry succeeds, transcription resumes. If it doesn't, after
3 attempts the user transitions to `fallback`.

Server-side knob to prevent this: refuse new WS upgrades when
`available_slots < 1`. Better to fail fast at connect than to accept
and then ship a degraded experience.

### Network is flaky on client side

Audio frames sit in the client's outbound queue. WS reconnects replay
them on next connection. If the queue overflows (>30s buffered, network
hasn't come back), drop oldest frames and surface a "Slow network"
indicator. Chunks pipeline (separate connection, REST POSTs) continues
on its own retry budget.

### Backend uvicorn restart

This is the existing per-request problem we have today with
`BackgroundTasks`-based reprocess (documented in
`feedback_brigade_llm_proxy_chain.md`). For WS, the same issue applies:
the open socket dies when uvicorn dies.

Mitigations:

1. **No mid-session redeploys.** Deploy windows go in a runbook;
   `docker compose up -d` of the backend during business hours is a
   no-go. If we have to redeploy, we drain WS connections first by
   sending `error.code=internal, retryable=true, retry_after_ms=30000`,
   wait 30s, then restart. The clients reconnect after the deploy.
2. **Health endpoint reports an empty drain flag.** Frontend polls
   `/api/health` once a minute on background tabs; if the flag is set,
   the active session stays connected but new sessions warn before
   starting.
3. **Eventually, sticky-session WS replay through Redis.** Documented
   as a Phase B.5+ option in Section 12. The path: on uvicorn restart,
   the client reconnects to whichever worker is up, the server pulls
   the session's audio ring + transcript state from Redis (per
   ~5s snapshots), and resumes. Out of scope for B.1-B.4; B.5
   production-polish considers it.

### Session is reassigned to a different org mid-meeting

The session_move_org feature lets admins move sessions between orgs.
If this happens during an active WS session, the WS user's org no
longer matches the session's org, and subsequent reads from the live
transcript would 403. Server detects the org change on its next
auth-refresh tick and closes the WS with `tier_revoked` (slight
overload of the message type — really it's "you no longer own this
session"). Client transitions to fallback.

### Privacy mode toggled mid-session (impossible by design)

Privacy mode locks at session start (Phase A.5 shipped this). Toggling
it mid-session via the settings page or another tab does not apply to
the current session. The check on WS open includes the session's
locked privacy state; even if the user changes the global toggle, the
in-progress session keeps its locked state.

### Tier revoked mid-session (Stripe webhook fires, etc.)

Server closes WS with `tier_revoked`. Client transitions to fallback.
Chunks pipeline continues; audio is still saved; canonical transcript
still produces. User sees "Live captions ended (subscription expired).
Your transcript is still being saved." Their session does not die.

### Audio device changes mid-session

The recorder already handles this (Phase A.5 shipped device-change
detection). When the underlying MediaStream gets replaced, we destroy
the old AudioWorklet, create a new one on the new stream, and
restart the audio frame loop. The WS sequence numbers continue
monotonically; the server doesn't need to know.

## 10. Privacy mode interaction

Server-live and privacy mode are mutually exclusive. The reason is
definitional: server-live streams audio bytes to the server in real
time; privacy mode means audio bytes never leave the device.

The mode-picker UI surfaces this directly. The user picks one of
(server-live, browser-live, privacy, capture-only) at session start
and the choice locks.

Mid-session, the user CAN open the settings panel and toggle the global
privacy preference. That toggle does not retroactively apply to the
current session — it sets the default for the next session.

Pro users on mobile **can** still choose privacy mode. They forfeit
live captions (the device probably can't run the local stack reliably
in a phone browser anyway), but A.6's post-stop local pass still runs
when the device can support it. The privacy-mode option copy in the
mode picker calls this out: "Privacy: audio never leaves your device.
On mobile, captions and summary will appear after you stop the
recording, about 2-5 minutes for a 30-minute meeting."

Free users on mobile see privacy mode as available too. They just
don't see server-live as an option. Their choice is privacy /
capture-only on mobile or privacy / browser-live on desktop-capable.

This means the same `recording_sessions.privacy_mode` flag still
governs the entire pipeline. Server reprocess is skipped when it's
true. Brigade graph push is skipped when it's true. The chunks
endpoint is skipped when it's true. Phase B just adds one more
"and the WS endpoint refuses to connect when it's true" check.

## 11. Implementation plan (phased breakdown)

Phase B is **~10-12 days of focused work** split into five sub-phases.
Each sub-phase ships independently. We can pause between any two if
something else (Brigade integration follow-ups, native iOS C-1 kickoff,
production hardening) takes priority.

### Phase B.1 — Minimal server WS endpoint + Parakeet streaming (~2-3 days)

- Stand up `meet-parakeet-stream-svc` on midboy2 GPU 0
  (Parakeet 0.6B v3 streaming, NeMo streaming mode or sliding-window
  fallback per Section 5 / Section 12). Verify via curl/wscat at ~80%
  of the latency budget.
- New `backend/api/websocket_live_server_stream.py` with the
  `LiveStreamSession` orchestrator (Section 5). Audio ring buffer.
  Parakeet streaming client. Outbound queue. No summary yet.
- Tier gate: hardcoded `is_pro_or_enterprise(user)` helper that
  checks against a temporary allowlist env var (until B.4 lands).
- Wire contract end-to-end: client sends PCM16, server replies with
  `transcript_partial` + `transcript_final`.
- Smoke test on midboy1 / midboy2 via a CLI client (`scripts/
  live_stream_smoke_test.py`) that reads a WAV and POSTs frames.

Acceptance: a `wscat` from a workstation can connect to the WS,
push pcm16 frames, and receive partials in <1.5s p95.

### Phase B.2 — Rolling summary via Qwen 3.6 (~2-3 days)

- Add summary scheduler to `LiveStreamSession`. Trigger every 5s or
  200 words.
- Reuse `services/summary_slices.py` Qwen call path. Update the
  prompt template per Section 5. Same `triggered_by='live-stream'`
  tag for the slice rows.
- Wire `summary_delta` outbound on the WS.
- Smoke test: a 5-minute test WAV produces ~12 summary deltas;
  the final delta matches what `triggered_by='auto-words'` produces
  via the chunked path.
- Bonus: emit `triggered_by='live-stream'` slice rows so the
  SessionDetails timeline page (when it exists) shows the rolling
  summary's evolution.

Acceptance: same smoke test as B.1, plus the WS also pushes summary
deltas with shape `{"type": "summary_delta", "summary_md": "..."}`
at the right cadence.

### Phase B.3 — Frontend WS client + UI integration (~2-3 days)

- New `frontend/src/services/liveStreamClient.ts` per Section 6.
- New AudioWorklet `pcm-encoder.js` for PCM16 transcoding.
- New `AlwaysOnContext` state + methods per Section 6. Wire
  `enableServerLive()` / `disableServerLive()`. Mode picker.
- `AlwaysOnControl.tsx` Live mode pill + pane substitution.
- `MobileLiveRecording.tsx` server-live integration: replaces the
  capture-only banner with the live pane when connected, falls back
  to capture-only banner when disconnected.
- Fallback messaging: "Live captions disconnected" / "Subscription
  expired" / "Server busy."

Acceptance: a Pro user on a real iPhone can open a session, click
"Server-live," see live captions and a rolling summary within 1
second of speaking, and seamlessly fall back to capture-only if the
WS drops three times.

### Phase B.4 — Tier gating + entitlement check (~1 day)

- Add `tier` + `tier_source` + `tier_updated_at` columns to
  `auth.models.User`. Default existing users to `'pro'` until pricing
  ships (Aaron's call — alternative is default `'free'` and manually
  promote his test accounts).
- `/api/auth/me` returns `tier` + `tier_features` per Section 3.
- `is_pro_or_enterprise(user)` helper replaced by
  `user.tier_features['server_live']`.
- Frontend reads `tier_features.server_live` from `/api/auth/me` and
  hides / shows the server-live UI accordingly.
- Free mobile users: capture-only banner + one-line upgrade nudge.
- Mid-session `tier_revoked` flow: server closes WS when tier check
  fails; client renders the "Live captions ended" banner.

Acceptance: a Free user on iPhone sees no server-live option and
the upgrade nudge. A Pro user on iPhone sees server-live and it
works. Manually flipping the DB tier to `'free'` mid-session closes
the WS within 60 seconds.

### Phase B.5 — Production polish (reconnect, backpressure, monitoring) (~2-3 days)

- Exponential backoff reconnect with sequence-replay (Section 4).
- Backpressure: `slot_status` frames, `error.code=backpressure` /
  `gpu_saturated` handling, server-side WS connect refusal when
  saturated.
- Keepalive both directions (30s) to keep CF / oauth2-proxy happy.
- Telemetry: client posts `live-metrics` at session end, server
  aggregates to Prometheus. Grafana dashboard for first-word latency,
  p50/p95 partial latency, reconnects, fallback rate.
- Deploy-window discipline: a `/api/health` drain flag, frontend
  reads it, drain WSs gracefully on deploy.

Acceptance: a 4-hour soak test with 5 concurrent fake clients sees
zero crashes, p95 partial latency <1500ms, reconnect rate <2/hour
per client. Grafana shows the live metrics. Drain flag confirmed
working in staging.

### Phase B.5+ (deferred): Redis pub/sub state replay

Documented as an option in Section 12. Defer to a later pass; not
required for B.1-B.5 ship.

### Total

**~10-12 working days** of focused work for B.1 through B.5. Less
if any sub-phase comes in cleaner than expected; more if Parakeet
streaming doesn't work out of the box (Section 12).

## 12. Open questions and deferred decisions

These need Aaron's input or active investigation before the
implementation phases above can start with confidence. Listed in
rough order of "blocks B.1" → "blocks later phases."

### Q1 — Does parakeet-server today expose a streaming interface?

**Resolved 2026-05-22**: New `meet-parakeet-stream-svc` container with Parakeet 0.6B v3 on midboy2 GPU 0. The existing 1.1B batch service on bigboy stays put as the canonical quality pass at session end. Long-term separation of live vs batch is the right architectural call; live and batch have fundamentally different latency/throughput profiles and tuning one breaks the other.

The shipped `meet-parakeet-svc` containers on midboy2 + bigboy are
configured for batch transcription. NeMo's Parakeet TDT family
*supports* streaming with finalized + draft tokens via configurable
left+right context, per the NVIDIA Modal example and the Parakeet
MLX implementation. We need to:

a. Inspect the current container's NeMo version + initialization to
   see whether the streaming mode can be enabled in-place via a
   config flag.
b. If not: build a separate `meet-parakeet-stream-svc` container with
   Parakeet 0.6B v3 in streaming mode. Stand it up on midboy2 GPU 0
   next to the existing 1.1B batch service.
c. Fallback if (a) and (b) both stall: Option A from Section 5
   (sliding 1.0-1.5s windows on the existing batch endpoint). Higher
   latency, repeated work, but reuses the shipped infra.

**Recommendation: assume (b), allocate a half-day to validate before
B.1.** A new container is cleaner than retrofitting the batch
service.

### Q2 — What does Free see on mobile? Default tier for existing users?

**Resolved 2026-05-22**: New users default to `tier='free'`. Aaron + Shafen set to `tier='enterprise'` + `is_superuser=true` (founder/admin) in the alembic migration via explicit UPDATE on their email rows. Tier and admin are orthogonal — admin bypasses tier gates entirely. Existing testers beyond Aaron + Shafen stay free until manually promoted.

The shipped users were enrolled before tier was a concept. Two
options for the migration:

- Default all existing users to `pro` until pricing launches. Aaron's
  team and existing testers don't lose features.
- Default all existing users to `free`, manually promote Aaron + the
  ops team to `pro`. Forces the UI gating to be real from day 1 in
  staging.

Recommendation: **second option** for the migration but with a
release-gate that Phase B can't ship to unicorncommander.ai until the
pricing decision lands. On magicunicorn.dev (dev tier), Aaron + ops
team are `'pro'`, everyone else is `'free'`, and the gating is
visibly real. Confirms Aaron's call.

### Q3 — Redis pub/sub vs sticky routing for WS scaling

**Resolved 2026-05-22**: Sticky routing for B.1-B.4. The existing `/ws/audio-levels` endpoint runs in production today on `meetingops.magicunicorn.dev` without Redis pub/sub, proving the single-uvicorn-worker pattern works for our scale. Add Redis pub/sub only when we scale horizontally past one backend container instance.

Right now we have one uvicorn worker per backend container. Phase B
adds long-lived WS connections. Two scaling shapes:

- **Sticky routing**: Traefik IPHash / consistent LB so each session
  hits the same worker on reconnect. Simple, no extra infra. Works
  until we need to scale horizontally past one container instance.
- **Redis pub/sub**: workers cross-publish messages so any worker can
  serve any reconnect. Adds a Redis dependency. Buys clean horizontal
  scaling later when we have multiple replicas.

We have a Redis instance already (used for the audio levels WS, see
`websocket_transcription.py`). Cost of adding pub/sub is low. But
the WS handler design from Section 5 doesn't *need* pub/sub for a
single backend instance.

Recommendation: **sticky routing for B.1-B.4, defer Redis pub/sub to
B.5+.** Aaron called this out in the brief as something to consider.
The right time to do it is when we add a second backend replica, not
before.

### Q4 — oauth2-proxy WS quirk in Cloudflare-Apex setups

**Resolved 2026-05-22**: No new seam to test. `/ws/audio-levels` is already in production through Cloudflare → oauth2-proxy → Traefik → meet-backend (visible in oauth2-proxy logs as recently as today during the Shafen auth investigation). Same pattern for `/ws/sessions/{id}/live`. Smoke-test deferred until B.1 implementation since the integration path is proven.

Section 4 covers the cookie flow. The known sharp edge from the
GitHub issue #1347 in the oauth2-proxy tracker is: after a WS upgrade
fails (e.g., upstream returns 404), reusing the connection for HTTP
requests strips the Authorization Bearer header and only forwards the
cookie. This doesn't affect our setup directly (we don't reuse WS
connections for HTTP), but it tells us the WS path through
oauth2-proxy has been thinly trodden.

Recommendation: **smoke-test the WS through the production
Cloudflare → oauth2-proxy → Traefik → FastAPI path on
meetingops.magicunicorn.dev before B.1's "scaffold and verify
locally" step.** If something is going to break, it'll break at the
CF + oauth2-proxy seam, not at our FastAPI handler. Validate first;
build later.

### Q5 — Sortformer streaming diarization: which phase?

Section 5 defaults to Choice 1 (defer diarization to session end) for
B.1 ship. Sortformer streaming (Choice 3) lands in Phase B.2 or B.3.

- B.2 is the LLM rolling-summary phase. Adding Sortformer is
  orthogonal, fits scope.
- B.3 is the frontend phase. Could fit there if Sortformer ships with
  a speaker-color picker UI for the live transcript bubbles.

Recommendation: **B.3.** B.2 is already tight at 2-3 days; pushing
Sortformer to B.3 lets the frontend phase ship the colored bubbles
in the same UI pass. If B.3 comes in over-budget, defer Sortformer
to a B.3.5 mini-phase rather than slipping the whole frontend ship.

### Q6 — Do native iOS / Android (C-1 / C-3) hit the same WS endpoint?

Strong yes, with one wrinkle. The WS protocol from Section 4 is
language-agnostic (PCM16 binary + JSON text frames). A Swift / Kotlin
client speaks it as easily as a TypeScript client. Reusing the
endpoint saves us from maintaining two streaming pipelines.

The wrinkle is auth. Browser clients ride the oauth2-proxy cookie.
Native clients don't have a cookie; they have a Bearer JWT obtained
through Keycloak's native flow. The WS handshake supports this via
`Sec-WebSocket-Protocol: bearer.<JWT>` (per RFC 6455 subprotocol
negotiation). Backend already trusts JWTs via the existing
`get_current_user_optional` path (`backend/auth/dependencies.py`).
We just add a WS-specific dependency that extracts the JWT from
either the cookie or the Sec-WebSocket-Protocol header.

Recommendation: **WS protocol is shared.** Native clients
(C-1 / C-2 watchOS / C-3) use the same `/ws/sessions/{id}/live`
endpoint with `Sec-WebSocket-Protocol: bearer.<JWT>`. Document this
in Phase B.4's auth section as the forward-compatible auth path.

### Q7 — Cost ceiling: do we ever throttle Pro users?

Hypothetically a Pro user could run 8 hours of live meetings every
day, generating ~240 hours of GPU streaming time per month. At our
amortized rate, that's still tiny — $5-10 of marginal compute. Not
worth metering for the first 1000 Pro users.

Past 1000 Pro users, we might want a "fair use" cap (e.g., 100
hours/month live, after which you fall back to browser-live or
capture-only). But that's a 2027 problem, not a Phase B problem.

Recommendation: **no throttle in B.1-B.5.** Add `usage_hours` to
`auth.models.User` as a counter we increment in B.5, so we have the
data when the conversation becomes relevant. Don't gate on it.

### Q8 — Where does the "Pro" tier name actually appear in the product?

Not a technical question, but a product copy decision. The brief uses
"Pro" and "Enterprise"; `docs/compute-economics.md` uses the same.
The mode-picker copy in Section 3 says "Server-live: fastest captions
on any device" without using the tier name; the upgrade nudge says
"Upgrade to Pro" but that's where the tier name surfaces.

Recommendation: **tier name = "Pro" everywhere user-facing,
"server-live" everywhere developer-facing.** Same way "Privacy mode"
is the user-facing name and `privacy_mode` is the column. Phase B.4
naming should match the existing patterns.

---

## Appendix A — Wire contract reference

Quick reference for the WebSocket message shapes from Section 4.

### Binary frame layout

Reconciled in Phase B.2 to the fixed 19-byte big-endian header that
ships in `backend/api/streaming.py` (struct format `>IQ4sHB`) and
`frontend/src/pages/StreamingTest.tsx`. See Section 4 for the
rationale.

```
Offset  Length  Field
0       4       sequence_number       (uint32, big-endian)
4       8       client_timestamp_us   (uint64, big-endian; microseconds)
12      4       payload_format        (4-char ASCII)
                  "PC16" = 16 kHz mono signed-LE PCM16
                  "OPUS" = OggOpus
                  "AACL" = AAC-LC
16      2       sample_rate / 100     (uint16, big-endian; 160 = 16 kHz)
18      1       flags                 (uint8; bit 0 = is_final)
19      N       payload bytes
```

### Text frame types (client → server)

- `hello` — initial handshake. Includes `session_id`, `client_version`,
  `device_capability`, `preferred_summary_cadence_sec`.
- `ping` — keepalive. Server replies with `pong`.
- `stop` — graceful close. Server emits any final partials, then
  closes.
- `pause` / `resume` — audio is paused/resumed client-side. Server
  pauses the parakeet stream and the summary scheduler.
- `audio_meta` — sample rate / channel / format change.

### Text frame types (server → client)

- `ready` — handshake response. Lists transcribe / summary / diarize
  providers actually in use.
- `transcript_partial` — unstable transcript fragment.
- `transcript_final` — locked transcript fragment.
- `summary_delta` — full rendered Markdown summary up through a
  timestamp.
- `slot_status` — server load. Informational.
- `keepalive` — server-side keepalive.
- `pong` — ping response.
- `error` — error frame, with retryable / retry_after.
- `tier_revoked` — close imminent due to entitlement change.

## Appendix B — Glossary

- **Capture-only**: Phase A mode where the device only uploads chunks;
  no live transcript or summary. Shipped in v0.8.0.
- **Browser-live**: Phase A.5/A.6 mode where the browser runs Parakeet
  0.6B INT8 + Qwen 3 0.6B INT8 locally. Shipped in v0.8.0.
- **Server-live**: Phase B mode where audio streams over a WebSocket
  to server-side Parakeet + Qwen pipelines. This doc.
- **Privacy mode**: a per-session toggle that keeps audio bytes on
  device. Mutually exclusive with server-live. Compatible with
  capture-only-local (Phase A.6) and browser-live (Phase A.5/A.6).
- **Server reprocess**: the post-stop pipeline (Parakeet 1.1B fp16 +
  pyannote 3.1 + wespeaker + Qwen 3.6 35B-A3B-Vision final summary).
  Shipped in v0.7.4.
- **Tier**: `'free'` / `'pro'` / `'enterprise'`. Per-user. Drives
  `tier_features`. New in Phase B.
