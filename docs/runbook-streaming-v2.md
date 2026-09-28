# Runbook: v2.x live streaming stack

Captures operational knowledge from the 2026-05-26 shipping arc that took
UC-Meeting-Ops from v1.0.0 to v2.1.0. For full design context see
`docs/phase-b-server-live-streaming.md`,
`docs/phase-b3-sortformer-spike.md`, `docs/phase-b3-nemo-streaming-spike.md`,
and `docs/phase-b3-integration-plan.md`. This doc is the **operations**
view — how to deploy, roll back, smoke-test, and inspect logs when
things behave unexpectedly.

## Container topology

| Service | Host | Port | Image tag | Purpose |
|---|---|---|---|---|
| `meet-backend` | bigboy | 9050 internal | `meet-backend:local` | FastAPI app, WS forwarder `/ws/sessions/{id}/live` |
| `meet-frontend` | bigboy | 80 via oauth2-proxy | `meet-frontend:local` | Nginx + Vite SPA |
| `meet-oauth2-proxy` | bigboy | 80/443 | upstream | OIDC gate via Keycloak `uchub` realm |
| `meet-bulk-import-worker` | bigboy | n/a | `meet-backend:local` | Arq + Redis bulk import queue |
| `meet-parakeet-svc` | midboy2 | 8881 | `meet-parakeet-svc:local` | Parakeet 1.1B batch (canonical /finalize-audio) |
| `meet-parakeet-stream-svc` | midboy2 | 8895 | `meet-parakeet-stream-svc:local` | Parakeet realtime-EOU 120M streaming |
| `meet-speaker-svc` | midboy2 | 8889 | `meet-speaker-svc:local` | Pyannote + wespeaker canonical diarization |
| `meet-sortformer-svc` | midboy2 | 8896 | `meet-sortformer-svc:local` | NVIDIA Sortformer 4-speaker live diarization |

Sortformer + parakeet-stream-svc were added in v1.4.0/v1.5.0/v2.0.0.

## Env flags that gate v2.x behavior

All live in `/srv/meeting-ops/deploy/bigboy/.env.bigboy`
and are forwarded to `meet-backend` via the explicit list in
`docker-compose.bigboy.yml` (env-file alone is NOT enough; each var
must also appear in the service's `environment:` block).

| Flag | Default | What it does | Set to 1 in production |
|---|---|---|---|
| `STREAMING_USE_V2_PARAKEET` | `0` | Routes WS audio to `/transcribe-stream-v2` (per-word tokens) instead of `/transcribe-stream` (single-text per window) | yes |
| `STREAMING_USE_SORTFORMER` | `0` | Fires sortformer-svc `/diarize-stream` in parallel with parakeet on every flush; merges `speakers` into outbound JSON | yes |
| `BRIGADE_API_KEY` | unset | Auth token for Brigade FalkorDB writes at session-complete. Without it, brigade_client is log-only no-op | set (from Brigade ADMIN_KEY) |
| `STREAM_VAD_ENABLED` | `1` | RMS-based silence-gate that skips parakeet calls on quiet windows | `1` |
| `STREAM_VAD_RMS_THRESHOLD` | `200` | PCM16 RMS threshold below which a window is treated as silence | `200` |
| `STREAM_LOOKBACK_SECONDS` | `1.0` | Word-boundary overlap on `take_pcm` cursor-based windowing | `1.0` |
| `SORTFORMER_MIN_SEGMENT_MS` | `500` | Drop sortformer speaker turns shorter than this — cleans up brief background-noise false positives | `500` |
| `SORTFORMER_URL` | `http://meet-sortformer-svc:8896` | midboy2 Tailscale IP for sortformer-svc | default |
| `PARAKEET_STREAM_URL` | `http://meet-parakeet-stream-svc:8895` | midboy2 Tailscale IP for streaming Parakeet | default |
| `PARAKEET_STREAM_MODEL` | `nvidia/parakeet_realtime_eou_120m-v1` | Streaming-trained EOU checkpoint (swapped from `parakeet-tdt-0.6b-v3` in v1.5.0). Set on midboy2 compose. | default |

## Deploy procedures

### Backend code change

```bash
ssh <deploy-host> 'cd /srv/meeting-ops/deploy/bigboy && \
  docker compose --env-file .env.bigboy -f docker-compose.bigboy.yml build backend && \
  docker compose --env-file .env.bigboy -f docker-compose.bigboy.yml up -d --force-recreate backend'
```

~24 s build, ~10 s startup. Backend has no healthcheck so verify via:

```bash
ssh <deploy-host> 'docker logs meet-backend --since 30s | grep -E "startup complete|ERROR"'
```

Should see `Application startup complete.` and no errors.

### Frontend code change

Same pattern with `frontend` instead of `backend`. Docker build now
enforces `tsc -b` strict per v1.1.0; tsc errors fail the build.

### Sortformer / parakeet-stream svc code change

1. Build on bigboy: `docker build -t meet-sortformer-svc:local services/sortformer-svc/` (or `services/parakeet-stream-svc/`)
2. Ship to midboy2: `ssh <deploy-host> 'docker save meet-sortformer-svc:local | ssh midboy2 docker load'` — takes 5-10 min for 22 GB images over Tailscale (most of the image is the shared meet-parakeet-svc base layer)
3. Restart on midboy2: `ssh <deploy-host> 'ssh midboy2 "cd /srv/uc-meeting-ops-deploy/midboy2 && docker compose -f docker-compose.midboy2.yml up -d --force-recreate meet-sortformer-svc"'`
4. Wait for health: ~15-30 s (model load from /models volume cache)

### midboy2 compose change (model swap, env override)

The midboy2 compose lives on midboy2 host, NOT in the git repo:
`/srv/uc-meeting-ops-deploy/midboy2/docker-compose.midboy2.yml`.
Convention: backup with `.bak.task-name-pre` suffix before editing.

```bash
ssh <deploy-host> 'ssh midboy2 "cp /srv/uc-meeting-ops-deploy/midboy2/docker-compose.midboy2.yml /srv/uc-meeting-ops-deploy/midboy2/docker-compose.midboy2.yml.bak.$(date +%Y-%m-%d)"'
```

## Rollback paths

### v2.0.0+ (per-word streaming)

`STREAMING_USE_V2_PARAKEET=0` in `.env.bigboy` + force-recreate backend.
Backend reverts to v1 endpoint `/transcribe-stream`. Frontend
`<ServerLiveTranscript>` auto-falls-back to the legacy per-frame view
when partials carry no `tokens_finalized`.

### v1.5.0 (model swap)

In `midboy2:/srv/uc-meeting-ops-deploy/midboy2/docker-compose.midboy2.yml`,
change `PARAKEET_STREAM_MODEL` default to `nvidia/parakeet-tdt-0.6b-v3`
+ restart `meet-parakeet-stream-svc`. The image already supports both;
model picks at startup.

### v1.4.0 (sortformer parallel dispatch)

`STREAMING_USE_SORTFORMER=0` in `.env.bigboy` + force-recreate backend.
Sortformer-svc keeps running (cheap to keep idle, ~244 MiB resident);
backend just stops dispatching to it.

### v1.3.0 (cursor + VAD workarounds)

`STREAM_VAD_ENABLED=0` to disable the silence gate; cursor-based
windowing can be reverted by editing `_SessionState.consumed_through_ms`
out of `take_pcm`. With v1.5.0's EOU model both workarounds should fire
much less often; full removal queued for v3.0.0 after telemetry confirms.

## Smoke tests

### End-to-end browser smoke

1. Open `https://meetingops.magicunicorn.dev/#/record` (HashRouter — the
   `#/streaming-test` form is admin diagnostic)
2. Click Start recording
3. Speak for 10-30 s; watch "Server live transcript" pane below
4. Verify: utterances appear, speaker chips render, EOU gray chip
   appears when you pause talking, faint italic suffix shows in-flight
   draft
5. Stop recording
6. SessionDetails should populate with full canonical transcript
   within 30-90 s

### Headless / programmatic smoke

```bash
# Sortformer service direct test
ssh <deploy-host> 'ssh midboy2 "docker cp meet-speaker-svc:/app/test_fixtures/synthetic_2speaker.wav /tmp/synthetic.wav 2>&1 | tail -2 ; curl -s -X POST -H \"Content-Type: audio/wav\" --data-binary @/tmp/synthetic.wav http://localhost:8896/diarize-stream | python3 -m json.tool"'
# Expect: speakers list with spk_id 0 + 1, distinct_speaker_count=2

# Parakeet stream-svc with EOU model
ssh <deploy-host> 'ssh midboy2 "curl -s http://localhost:8895/healthz | python3 -m json.tool"'
# Expect: model=nvidia/parakeet_realtime_eou_120m-v1, model_loaded=true

# Backend module-level flags
ssh <deploy-host> 'docker exec meet-backend python3 -c "from api.streaming import STREAMING_USE_V2_PARAKEET, STREAMING_USE_SORTFORMER, SORTFORMER_URL, PARAKEET_STREAM_URL; print(f\"v2={STREAMING_USE_V2_PARAKEET} sort={STREAMING_USE_SORTFORMER} sort_url={SORTFORMER_URL} stream_url={PARAKEET_STREAM_URL}\")"'
# Expect: v2=True sort=True sort_url=http://meet-sortformer-svc:8896 stream_url=http://meet-parakeet-stream-svc:8895

# Brigade integration live check
ssh <deploy-host> 'docker exec meet-backend python3 -c "from services.brigade_client import _api_key, _base_url; k=_api_key(); print(f\"is_live={bool(k)} key_prefix={k[:8] if k else None} base_url={_base_url()}\")"'
# Expect: is_live=True key_prefix=363d6313 base_url=http://unicorn-brigade:8100
```

## Sortformer canonical-hybrid diarization (v2.2.0, opt-in / PARKED)

A post-hoc `/finalize-audio` diarization path where Sortformer draws the
speaker boundaries and wespeaker (via `meet-speaker-svc`) supplies the
per-turn embeddings. **Default is `pyannote`; this is OFF in production.**

**Status: parked, short-audio-only.** Sortformer v1 is trained on ~90 s
sessions and its one-shot `diarize()` is ~quadratic in audio length — a
38-min meeting needs ~25 GiB and OOMs the 12 GB 3060 (a 24 GB card won't
fit it either). Fine on short clips. Do NOT enable for orgs with
real-length meetings. See CHANGELOG 2.2.0 "Known limitations" for the
three promotion options (chunk+stitch / streaming-state API / NeMo 2.5).

Enable (per-org, preferred — flip one org for validation):
```sql
-- set provider_name on the org's diarization OrgProviderSettings row
UPDATE org_provider_settings SET provider_name='sortformer-hybrid'
  WHERE organization_id=<id> AND service_kind='diarization';
```
Or globally via env (affects all orgs): set
`SPEAKER_PROVIDER_PREFERENCE=sortformer-hybrid` in `.env.bigboy`, add it to
the backend `environment:` block, force-recreate backend. Revert by
removing it (default falls back to pyannote).

Smoke the svc endpoint directly (short fixture):
```bash
ssh <deploy-host> 'ssh midboy2 "docker cp meet-speaker-svc:/app/test_fixtures/synthetic_2speaker.wav /tmp/syn.wav; \
  curl -s -X POST -F audio=@/tmp/syn.wav -F return_embeddings=true \
  http://localhost:8896/diarize-file-upload | python3 -m json.tool | head -20"'
# Expect: backend=sortformer-hybrid, num_speakers=2, segments with
#         speaker=SPEAKER_NN + a 256-float embedding on long-enough turns.
```
On OOM the svc returns HTTP 500 and the provider's `diarize()` returns `[]`
— the reprocess keeps the live transcript with no speaker overlay (no
automatic pyannote fallback in 2.2.0).

## Common failure modes + fixes

### WS connect fails with code 1006 every time

v1.2.2 fix for `_resolve_org_bucket` should have eliminated this. If it
returns, check:

```bash
ssh <deploy-host> 'docker logs meet-backend --since 5m | grep -E "DetachedInstanceError|session_live"'
```

If you see `DetachedInstanceError`, the v1.2.2 fix may have regressed.
Check that the `try/except DetachedInstanceError` block in
`_resolve_org_bucket` is intact at `backend/api/streaming.py`.

### Live transcript shows the same words repeating

This was the v1.3.0 cursor symptom. If it returns despite v1.5.0's EOU
model, check that `STREAM_VAD_ENABLED=1` AND that `_SessionState`'s
`consumed_through_ms` cursor is being advanced (look for the
`state.consumed_through_ms = cumulative_at_call` lines in
`_flush_to_stt`).

### Sortformer speaker chips never appear

1. Backend env flag: `STREAMING_USE_SORTFORMER=1`? Check via `docker exec meet-backend env | grep SORTFORMER`.
2. Sortformer-svc reachable? `ssh <deploy-host> 'curl -s http://meet-sortformer-svc:8896/health'`
3. Sortformer-svc model loaded? Health response should have `model_loaded: true`.
4. Backend logs: look for `[B.3 WS] sortformer_unreachable` or `sortformer_http_error` (logged via `docker logs meet-backend --since 5m | grep sortformer`).
5. If `SORTFORMER_MIN_SEGMENT_MS=500` is filtering all turns: lower temporarily to 100.

### Speaker-svc periodically restarts

By design. The `MAX_UPTIME_HOURS=24` env var in midboy2 compose
triggers a planned restart every ~24 h to flush the pyannote VRAM
leak (task #79). Synthetic-WAV probe runs every 4th healthcheck tick
(~20 min) to catch unplanned pipeline-state degradation (task #105).
Both are configured in the host-mounted
`/srv/uc-meeting-ops-deploy/midboy2/scripts/speaker_svc_healthcheck.py`.

### Sortformer over-segments solo audio (extra speaker chips on one person's text)

Tune `SORTFORMER_MIN_SEGMENT_MS` higher in `.env.bigboy` + restart
backend. Default 500 ms catches most brief noise; raise to 800 or 1000
if you see false-positive chips on quiet speakers.

### Frontend mid-recording resume banner unexpectedly

PWA service worker detected a frontend deploy and reloaded the SPA.
The recording state is safe (orphan-resume + IDB) but the user gets
interrupted. Not yet fixed at the workflow level — design needed for
when to show update banner vs auto-reload.

## Log inspection patterns

```bash
# Last 10 partial-transcript emissions
ssh <deploy-host> 'docker logs meet-backend --since 5m | grep -E "B.2 WS.*flush ok" | tail -10'

# VAD silence-gate firings (should be common during normal recordings)
ssh <deploy-host> 'docker logs meet-backend --since 30m | grep streaming-vad | tail -10'

# Sortformer dispatch failures (should be rare)
ssh <deploy-host> 'docker logs meet-backend --since 1h | grep -E "sortformer_(unreachable|http_error)"'

# Per-org rate limit hits
ssh <deploy-host> 'docker logs meet-backend --since 1h | grep "rate_limited"'

# Backpressure events (slow upstream)
ssh <deploy-host> 'docker logs meet-backend --since 1h | grep streaming-backpressure'

# Synthetic-WAV probe firings on speaker-svc
ssh <deploy-host> 'ssh midboy2 docker logs meet-speaker-svc --since 1h | grep healthz/synthetic'
```

## Prometheus metrics (exposed at backend `/metrics`)

- `meeting_ops_ws_connections_total{tier, result}` — connection attempts (tier=free/pro/enterprise/unknown, result=accepted/tier_rejected/auth_rejected/rate_limited)
- `meeting_ops_ws_audio_frames_forwarded_total` — flushes to parakeet-stream-svc
- `meeting_ops_ws_partial_transcripts_emitted_total` — partials emitted back to client
- `meeting_ops_ws_close_codes_total{code}` — WS close codes (1000=normal, 1001=going away, 1006=abnormal, 4001=auth, 4003=tier, 4429=rate-limited)
- `meeting_ops_parakeet_stream_request_duration_seconds` — histogram

## Test suite

Backend pytest: `329 passed, 0 failed, 3 skipped` in ~4 min as of v2.x
(was 318 at v1.1.0 baseline; the +11 is the new `test_streaming_v2.py`
file added in this session).

Streaming-specific tests:
- `backend/tests/test_streaming_polish.py` — Phase B.5 backpressure + rate limit + metrics + slow-upstream + active sessions
- `backend/tests/test_streaming_tier_gate.py` — Phase B.4 tier-gating (free→4003, pro/enterprise→accepted)
- `backend/tests/test_streaming_v2.py` — v1.2.2/v1.3.0/v1.5.0/v2.x unit tests (VAD silence, cursor advancement, DetachedInstanceError tolerance, v2 flag parsing)
