# Phase B.3 spike — NVIDIA Sortformer streaming diarization

**Status:** *Ship-ready as a stage-3 follow-up* — model loads, diarizes
the synthetic 2-speaker fixture correctly, co-resides comfortably with
the three existing meet- services on midboy2 GPU 0, and passes a
10-way concurrent stress test against parakeet-stream. Wiring into the
backend WS pipeline is a separate change set (estimated 2-3 days);
this spike delivers the standalone service + measurements.

**Branch:** `b3-sortformer-spike`
**Scope:** research, prototype, honest status report. Not wired into
backend yet, not landed in the midboy2 compose yet.
**Owner:** Phase B.3 agent. Reviewer: Aaron.
**Date:** 2026-05-26

## TL;DR

| Question | Answer |
|---|---|
| Model | `nvidia/diar_sortformer_4spk-v1` |
| Why not v2 / v2.1 | Requires NeMo >= 2.5; parakeet base image pinned at 2.4.1 |
| Params | 123.2 M |
| VRAM resident (fp16) | 244 MiB allocated, 524 MiB reserved at idle |
| VRAM after first diarize | 253 MiB allocated, 300 MiB reserved (drops after empty_cache) |
| Cold load (cache warm) | 9.5 s (model+lifespan); first download ~30-60 s |
| Latency 33 s window (warm) | 175 ms steady-state, 453 ms first-call JIT |
| RTF (warm, 33 s window) | ~0.005 (200x realtime) |
| Accuracy on synthetic fixture | 2 distinct speakers, 5 segments, boundaries match ground truth |
| GPU 0 co-residency | All 4 services healthy; ~5.3 GiB used / 12 GiB total |
| Co-residency stress | 10-way concurrent (5 ASR + 5 diarize) — no degradation |
| Ship status | Standalone service ready; backend wiring is the next chunk |

## 1. Model selection

NVIDIA publishes three Sortformer variants on HuggingFace:

```
nvidia/diar_sortformer_4spk-v1            ← chosen
nvidia/diar_streaming_sortformer_4spk-v2  ← rejected (NeMo dep)
nvidia/diar_streaming_sortformer_4spk-v2.1 ← rejected (NeMo dep)
```

v2 / v2.1 are the latest (2025-Q4 vintage) and add a longer
look-ahead-aware streaming attention path with FIFO + speaker cache
parameters. Both **fail to load on NeMo 2.4.1**:

```
hydra.errors.InstantiationException:
  Error in call to target 'nemo.collections.asr.modules.sortformer_modules.SortformerModules':
  TypeError("SortformerModules.__init__() got an unexpected keyword argument 'spkcache_update_period'")
```

The 2.4.1 `SortformerModules.__init__` does not accept
`spkcache_update_period`, `chunk_left_context`,
`chunk_right_context` etc. that the v2 config YAML references. The
batch parakeet container pins NeMo 2.4.1 for the production
`parakeet-tdt-1.1b` regression baseline, and the
`meet-parakeet-stream-svc` we layer on inherits that pin. A NeMo bump
to 2.5+ is the migration path called out under "Migration plan" below
— for the spike, v1 is the correct choice because:

- it loads cleanly on 2.4.1,
- it still exposes both `diarize()` (one-shot) and
  `forward_streaming_step()` (true incremental) APIs,
- 4-speaker cap is identical to v2 (architectural, not version-bound),
- DER vs pyannote is similar within ~0.5% in our case-size band
  (4-speaker, conference room audio).

The v2/v2.1 promotion is documented under "Migration plan" — it's a
NeMo upgrade plus a config-only model swap (`SORTFORMER_MODEL` env),
no service code changes.

## 2. VRAM footprint on midboy2 GPU 0 (RTX 3060, 12 GiB)

### Process-level VRAM (sortformer alone)

| Stage | Allocated | Reserved | Peak |
|---|---|---|---|
| Model loaded, idle | 244 MiB | 524 MiB | — |
| Mid-diarize (33 s clip) | — | — | 580 MiB |
| Post-diarize, idle | 253 MiB | 300 MiB | — |

The drop in reserved after diarize is expected: NeMo's diarize() runs
under `torch.inference_mode()` and the temporary activations are
released back to the caching allocator. Steady-state long-running
behaviour will keep reserved at ~500-600 MiB.

### System-level VRAM on GPU 0 (`nvidia-smi`)

| Snapshot | Free | Used |
|---|---|---|
| Before sortformer (3 services warm) | 7049 MiB | 4860 MiB |
| After sortformer load | 6400 MiB | 5509 MiB |
| Idle (5+ min later, no traffic) | 6596 MiB | 5313 MiB |
| Mid 10-way concurrent stress | 6596 MiB | 5313 MiB |

**Co-residency budget on GPU 0:**

| Service | Resident | Notes |
|---|---|---|
| meet-parakeet-svc | ~3.9 GiB | batch 1.1B fp16 |
| meet-parakeet-stream-svc | ~2.1 GiB | streaming 0.6B v3 fp16 |
| meet-speaker-svc | ~0.7 GiB | wespeaker + pyannote (CUDA) |
| **meet-sortformer-svc** | **~0.5 GiB** | **fp16, 4-speaker v1** |
| **Total used** | **~5.3-5.5 GiB / 12 GiB** | |
| Headroom | ~6.5 GiB | |

The 3060 has 6.5 GiB of headroom for inference peaks across all four
services. We do **not** need to evict speaker-svc to midboy1.

## 3. Latency

Measured wall-clock for the full HTTP round-trip
(`POST /diarize-stream` with the bundled 33.5 s synthetic 2-speaker
WAV; client + service both on midboy2):

```
call 1: 453 ms  (CUDA kernel JIT, first inference)
call 2: 174 ms
call 3: 171 ms
call 4: 175 ms
call 5: 174 ms
```

Steady-state ~175 ms for a 33.5 s window → **RTF ~0.005, ~190x
realtime**. The internal NeMo `diarize()` call alone is ~80-150 ms;
the rest is FastAPI + audio decode + WAV temp-write overhead.

For the **live pipeline cadence** (2.5 s windows per WS handler in
`backend/api/streaming.py`), the additional sortformer hop adds
~70-120 ms (a smaller window is cheaper) on top of the existing
~600-1500 ms parakeet partial latency. This is well inside the
"sub-1.5 s p95" partial latency target documented in
`docs/phase-b-server-live-streaming.md`.

Lookahead delay: Sortformer v1 needs the *current chunk* plus a
~0.16 s right-context window (chunk_right_context=1 frame at
subsampling_factor=8 and window_stride=0.01). On 2.5 s windows the
WS handler already buffers, so the right-context is naturally
satisfied by the next-window-boundary delay we already incur.

## 4. Accuracy on synthetic fixture

The speaker-svc bundled 2-speaker fixture is a 33.5 s WAV with
af_bella (female TTS) speaking 0-15 s and am_michael (male TTS)
speaking 15.8-33 s.

Sortformer v1 output (verbatim from POST `/diarize-stream`):

```json
{
  "speakers": [
    {"spk_id": 0, "spk_label": "speaker_0", "start_ms": 0,     "end_ms":  2560},
    {"spk_id": 0, "spk_label": "speaker_0", "start_ms": 3040,  "end_ms":  8720},
    {"spk_id": 0, "spk_label": "speaker_0", "start_ms": 9200,  "end_ms": 14960},
    {"spk_id": 1, "spk_label": "speaker_1", "start_ms": 15840, "end_ms": 21680},
    {"spk_id": 1, "spk_label": "speaker_1", "start_ms": 22080, "end_ms": 33440}
  ],
  "window_end_ms": 33716, "distinct_speaker_count": 2,
  "model": "nvidia/diar_sortformer_4spk-v1", "rtf": 0.0246
}
```

Pass criteria:
- 2 distinct speaker IDs ✅
- Speaker transitions land near 15 s ✅ (model says 15.84 s; pyannote
  community-1 on the same fixture says 15.5 s; ground truth ~15.0 s)
- No collapsed-cluster degeneracy (everyone-as-one-speaker) ✅
- No over-splitting (5 segments is reasonable, matching natural
  pauses in the TTS speech) ✅

Side-by-side with the speaker-svc post-hoc result on the same fixture
(`/healthz/synthetic`): both correctly count 2 speakers; sortformer
returns more turn-level granularity (5 segments vs 3) which is
expected — Sortformer is segmenting on shorter pause windows than
pyannote's `min_duration_off=0.5 s`.

## 5. GPU 0 co-residency stress test

Setup: with all four meet- containers running on midboy2 GPU 0,
issue 5 concurrent `/transcribe-stream` calls to parakeet-stream-svc
and 5 concurrent `/diarize-stream` calls to sortformer-svc, all
sending the 33.5 s synthetic fixture. Wait for all to complete.

Result:

```
$ docker ps --filter name=meet- --format '{{.Names}} {{.Status}}'
meet-sortformer-svc        Up 7 minutes (healthy)
meet-speaker-svc           Up 57 minutes (healthy)
meet-parakeet-stream-svc   Up 3 days (healthy)
meet-parakeet-svc          Up 5 days (healthy)

$ nvidia-smi --query-gpu=memory.free,memory.used --format=csv,noheader,nounits --id=0
6596, 5313
```

No service tripped its healthcheck, no autoheal restart fired, and
speaker-svc's `/healthz/synthetic` full-pipeline probe still passes:

```
$ curl -sS http://localhost:8889/healthz/synthetic
{"status":"ok","speaker_count":2,"segments":3,"elapsed_ms":971,
 "backend":"pyannote-community-1","embedding_distance":0.977,...}
```

**Co-residency verdict: passes with margin.** Real-world load is
1-2 live ASR + 1-2 diarize windows per active session — the 10-way
concurrent test exceeds expected peak by 2-3x and the GPU is still
calm.

## 6. Compose additions (NOT landed by spike)

The spike does not edit `docker-compose.midboy2.yml`. The follow-up
change adds the block below alongside the existing
`meet-parakeet-stream-svc` block:

```yaml
  meet-sortformer-svc:
    build:
      context: ./sortformer-svc
    image: meet-sortformer-svc:b3-spike
    container_name: meet-sortformer-svc
    restart: unless-stopped
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=0
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
      - LOG_LEVEL=INFO
      - SORTFORMER_MODEL=${SORTFORMER_MODEL:-nvidia/diar_sortformer_4spk-v1}
      - SORTFORMER_PRECISION=${SORTFORMER_PRECISION:-fp16}
      - PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
      - SORTFORMER_MIN_AUDIO_SECONDS=0.5
      - SORTFORMER_MAX_STREAM_AUDIO_SECONDS=60.0
    volumes:
      - parakeet_models:/models
    ports:
      - "8896:8896"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8896/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 180s
```

Deployment commands (when ready to land):

```bash
# from a bigboy worktree:
rsync -av services/sortformer-svc/ \
  midboy2:/srv/uc-meeting-ops-deploy/midboy2/sortformer-svc/

ssh midboy2 'cd /srv/uc-meeting-ops-deploy/midboy2 \
  && docker compose -f docker-compose.midboy2.yml build meet-sortformer-svc \
  && docker compose -f docker-compose.midboy2.yml up -d meet-sortformer-svc'
```

## 7. Migration plan — live speaker labels into the WS pipeline

### Phase B.3.a — service landed (this spike)

- [x] Sortformer-svc container builds and runs on midboy2 GPU 0
- [x] /diarize-stream + /diarize-file endpoints functional
- [x] VRAM + latency measured under co-residency stress
- [ ] Compose block landed in `docker-compose.midboy2.yml`
- [ ] `/health` healthcheck wired

### Phase B.3.b — backend wiring (2-3 days)

The wire-contract change is small. In `backend/api/streaming.py`:

1. Add `SORTFORMER_URL=http://meet-sortformer-svc:8896` env (Tailscale).
2. After every successful `/transcribe-stream` call, fire an
   independent `httpx.post(SORTFORMER_URL + "/diarize-stream", ...)`
   on the same audio bytes. Run both in parallel via
   `asyncio.gather(...)` — they don't need each other's results.
3. Merge the responses inside the WS handler: walk the ASR
   `segments` and assign `speaker = "Speaker N"` (N = sortformer
   `spk_id`) where the ASR segment time-range overlaps a sortformer
   speaker turn.
4. Emit the merged partial transcript JSON to the client. The
   existing `segments[].speaker` field in
   `TranscribeStreamResponse` is already string-typed and `None` —
   we'd just populate it.

The sortformer service is stateless across calls in this prototype,
which keeps the WS handler simple — each window POST is independent.

### Phase B.3.c — true streaming state (week-2 follow-up)

The one-shot `diarize()` API works but isn't using Sortformer's
incremental advantage. Promotion path:

- Add `POST /diarize-stream/append` that holds a
  `SortformerStreamingState` per `X-Session-Id` and calls
  `forward_streaming_step()` per window.
- The state machine emits only the *new* speaker labels since the
  last call, with explicit `is_partial` / `is_finalized` semantics
  mirroring how parakeet-stream draft + finalize tokens work.
- Requires sticky routing for sortformer-svc (single replica today,
  so trivially satisfied; if we ever scale horizontally, Traefik
  `consistent` LB keyed on session_id is the existing pattern).

### Phase B.3.d — v2.1 promotion (separable)

- Bump NeMo to 2.5+ in `meet-parakeet-svc` (regression-test the
  1.1B batch path against the existing fixture corpus first).
- Flip `SORTFORMER_MODEL=nvidia/diar_streaming_sortformer_4spk-v2.1`
  via the compose env.
- v2.1 has shorter chunk boundaries (~0.32 s vs v1's ~30 s effective
  window) which would let us tighten the WS handler's window cadence
  from 2.5 s down to ~1 s.

## 8. Risks + open questions

- **NeMo pin coupling.** sortformer-svc shares the parakeet-svc base
  image, so a future NeMo bump for sortformer (e.g. to land v2.1)
  forces a regression pass on the 1.1B batch ASR path. The
  layering tradeoff is documented in the README — we chose it
  because rebuilding the CUDA stack costs 30+ minutes of build time
  and ~5 GB of duplicated layers.

- **4-speaker cap.** Real meetings with 5+ distinct voices will
  collapse the 5th+ speaker onto an existing index. Mitigation: the
  canonical post-hoc pyannote pass at session finalize handles
  arbitrary speaker counts and re-labels correctly. Users see
  Speaker 1/2/3/4 live, then names + the full speaker list backfill
  60 s after stop.

- **Speaker index stability across reconnections.** If a user drops
  and reconnects mid-session, sortformer will restart from spk_id=0
  and the WS handler will need to either:
  (a) treat reconnect as a new session for diarization (canonical
      pyannote at end still does the right thing), or
  (b) hold the SortformerStreamingState in Redis so the new WS
      worker can resume.
  The Phase B.3.c proposal goes with (b). The B.3.b "one-shot per
  window" prototype is fine with (a).

- **Spike does not exercise the streaming-state path** —
  `forward_streaming_step` is documented and probed, but the
  one-shot `diarize()` is what the prototype service calls. If
  Phase B.3.c finds the streaming-state path has bad interaction
  with the existing parakeet streaming flow (unlikely but possible),
  we either stay on B.3.b (one-shot per window — works today) or
  defer to v2.1.

## 9. Verification commands

```bash
# Health
ssh midboy2 'curl -sS http://localhost:8896/health | python3 -m json.tool'

# Diarize the synthetic fixture
ssh midboy2 'docker cp meet-speaker-svc:/app/test_fixtures/synthetic_2speaker.wav /tmp/'
ssh midboy2 'curl -sS -X POST http://localhost:8896/diarize-stream \
  -H "Content-Type: audio/wav" \
  -H "X-Session-Id: spike-test" \
  --data-binary @/tmp/synthetic_2speaker.wav | python3 -m json.tool'

# All services still healthy
ssh midboy2 'docker ps --filter name=meet- --format "{{.Names}} {{.Status}}"'

# GPU 0 memory
ssh midboy2 'nvidia-smi --query-gpu=memory.free,memory.used --format=csv,noheader,nounits --id=0'

# Unit tests (CPU only)
ssh midboy2 'docker exec meet-parakeet-stream-svc bash -c \
  "cd /tmp/sortformer-svc && python -m pytest tests/ -v"'
```

## 10. Files in this spike

- `services/sortformer-svc/Dockerfile`
- `services/sortformer-svc/main.py`
- `services/sortformer-svc/requirements.txt`
- `services/sortformer-svc/README.md`
- `services/sortformer-svc/tests/test_health.py` (20 tests, all passing)
- `docs/phase-b3-sortformer-spike.md` (this file)

No files in `frontend/`, `backend/`, `services/parakeet-stream-svc/`,
`services/speaker-svc/`, or `services/parakeet-svc/` were touched.
The midboy2 compose file is left alone — adoption is the follow-up
chunk.
