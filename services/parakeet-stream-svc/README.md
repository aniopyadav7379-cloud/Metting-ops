# meet-parakeet-stream-svc

Streaming-ASR microservice for the Phase B server-live path. Real
NVIDIA Parakeet 0.6B v3 model in a NeMo + CUDA container, hosted on
midboy2 GPU 0 alongside the batch `meet-parakeet-svc` (which keeps
handling completed-audio reprocess on the 1.1B model).

## Status: Phase B.2 — real model

Shipped 2026-05-22:

- `GET /healthz` returns `model_loaded: true` after warm-up
  (~10-30 s from container start while NeMo loads weights into VRAM).
- `POST /transcribe-stream` accepts WAV bytes (or raw PCM16 LE at
  16 kHz mono), runs them through Parakeet 0.6B v3, and returns
  `{text, segments, words, duration, model, rtf, confidence,
  sequence, is_final}`.
- Default model: `nvidia/parakeet-tdt-0.6b-v3` (multilingual, 2026
  vintage). Falls back to `nvidia/parakeet-tdt-0.6b-v2` if v3 fails
  to load (e.g., HF download issues).
- fp16 on the 3060, ~1.4 GB resident.

## Why we layer on meet-parakeet-svc

The base image
[`meet-parakeet-svc:local`](../parakeet-svc/Dockerfile) already
ships CUDA 12.4 + NeMo 2.4.1 + torch 2.4.1 + the full audio tooling
chain. Rebuilding all of that from a CUDA base image for a 0.6B
variant would take 30+ minutes and ~5-8 GB of duplicated layers on
midboy2's already-tight `/var/lib/docker` (133 GB free, ~80 GB used).

Layering means:

- build is seconds (only a `pip install -r requirements-stream.txt`
  and a couple of file copies),
- the NeMo behaviour matches the batch service exactly (so a model
  bug surfaces in both surfaces and we fix once),
- the named volume `parakeet_models` is shared with the batch
  service, so once v3 is downloaded by either container the other
  also finds it in cache.

If we ever decouple (e.g., the batch service moves to a newer NeMo
that's incompatible with the streaming model), we copy
`services/parakeet-svc/Dockerfile` as a starting point and pin
versions independently. Until that day, layering is the right call.

## Coming in B.3

- True NeMo streaming via finalized + draft tokens
  (`model.streaming_step()` API) so partial latency drops from ~600 ms
  per window to ~250-400 ms first-word.
- Replace the WAV-in HTTP shape with a server-side stateful streaming
  endpoint (`POST /transcribe-stream/state` + repeated audio chunks
  to `POST /transcribe-stream/append`). The meet-backend WS handler
  changes from "every 2.5 s POST a WAV" to "stream chunks to a
  per-session NeMo state".
- Sortformer streaming diarization (Choice 3 in design doc section 5)
  on the same GPU. Adds ~1-1.5 GB VRAM; 3060 has room.

## Build + deploy on midboy2

Build context lives **on the midboy2 host** at
`/srv/uc-meeting-ops-deploy/midboy2/meet-parakeet-stream-svc/`,
rsynced from this repo. The runtime compose is sibling-folder:
`/srv/uc-meeting-ops-deploy/midboy2/docker-compose.midboy2.yml`.

```bash
# from a bigboy worktree:
rsync -av services/parakeet-stream-svc/ \
  midboy2:/srv/uc-meeting-ops-deploy/midboy2/meet-parakeet-stream-svc/

ssh midboy2 'cd /srv/uc-meeting-ops-deploy/midboy2 \
  && docker compose -f docker-compose.midboy2.yml build meet-parakeet-stream-svc \
  && docker compose -f docker-compose.midboy2.yml up -d meet-parakeet-stream-svc'
```

Verify:

```bash
ssh midboy2 'docker ps --format "{{.Names}} {{.Status}}" | grep parakeet-stream'
ssh midboy2 'curl -sS http://localhost:8895/healthz'
```

Expected after ~30 s warm-up:

```json
{"status":"ok","version":"0.2.0","model_loaded":true,
 "model":"nvidia/parakeet-tdt-0.6b-v3","precision":"fp16","phase":"B.2"}
```

End-to-end probe with the speaker-svc 2-speaker fixture:

```bash
ssh midboy2 'curl -sS -X POST http://localhost:8895/transcribe-stream \
  -H "Content-Type: audio/wav" \
  -H "X-Session-Id: probe" \
  --data-binary @/tmp/synthetic_2speaker.wav | python3 -m json.tool'
```

Expected: a `text` field containing the af_bella + am_michael
synthesized utterances (~33 s of speech, the model returns the
transcript for the whole window — no diarization, that's still
session-finalize).

## Networking

- Port: **8895** on midboy2 host (sits alongside
  `parakeet-svc:8881` and `speaker-svc:8889` in the 88xx band).
- The meet-backend on bigboy reaches it over Tailscale at
  `http://meet-parakeet-stream-svc:8895` (configurable via `PARAKEET_STREAM_URL`
  env var in the meet-backend container).
- No public ingress. Only client is the meet-backend WS handler in
  `backend/api/streaming.py`.

## VRAM budget

Measured on midboy2 GPU 0 (RTX 3060, 12 GB) with both batch +
stream containers warm:

| Service | Model | Precision | Resident VRAM |
|---|---|---|---|
| meet-parakeet-svc | parakeet-tdt-1.1b | fp16 | ~4.0 GB |
| meet-speaker-svc | wespeaker + community-1 | fp32 | ~2.0 GB |
| meet-parakeet-stream-svc | parakeet-tdt-0.6b-v3 | fp16 | ~1.4 GB |
| **Total** | | | **~7.4 GB / 12 GB** |

~4.6 GB headroom on GPU 0 — fine. Sortformer streaming diarization
in B.3 will eat ~1-1.5 GB more; still within budget.

## References

- Design doc: [`docs/phase-b-server-live-streaming.md`](../../docs/phase-b-server-live-streaming.md)
- Sibling service (batch): `services/parakeet-svc/`
- Sibling service (diarization): `services/speaker-svc/`
- Frontend test page: `frontend/src/pages/StreamingTest.tsx`
- meet-backend WS endpoint: `backend/api/streaming.py`
