# meet-sortformer-svc

Phase B.3 **spike** — NVIDIA Sortformer live speaker diarization
microservice. Targets midboy2 GPU 0 (RTX 3060, 12 GB) alongside the
existing parakeet-svc / parakeet-stream-svc / speaker-svc trio.

> **Status: research + prototype only.** Not wired into the live
> WS pipeline yet. See `docs/phase-b3-sortformer-spike.md` for the
> spike report, ship/no-ship verdict, and integration plan.

## Why this exists

Today live partial transcripts have no speaker labels —
meet-parakeet-stream-svc emits text, and the canonical speaker names
only backfill ~60 s after the session ends when meet-speaker-svc
(pyannote community-1 + wespeaker) processes the full session WAV.

Sortformer is the design-doc-blessed live diarization path
([`docs/phase-b-server-live-streaming.md`](../../docs/phase-b-server-live-streaming.md)
section 5, "Choice 3"). It emits stable 4-speaker indices on a
streaming feed with ~0.3 s lookahead and DER notably better than
pyannote on standard benchmarks.

## Model

`nvidia/diar_sortformer_4spk-v1`. v1 because the parakeet base image
we layer on ships NeMo 2.4.1, and the newer streaming v2 / v2.1
models reference a config field (`spkcache_update_period`) that the
2.4.1 `SortformerModules.__init__` does not accept. NeMo upgrade is
the migration path called out in the spike report.

v1 still supports `forward_streaming_step()` for true incremental
streaming, but the spike's POST surface uses the simpler one-shot
`diarize()` API on caller-managed windows to match the existing
parakeet-stream-svc contract one-for-one.

| Property | Value |
|---|---|
| Params | 123.2 M |
| VRAM resident (fp16) | ~500 MiB |
| VRAM peak diarize 30 s | ~580 MiB |
| Cold load (cache warm) | 2-3 s |
| Cold load (first download) | 30-60 s |
| Latency 33 s window | ~0.9 s (RTF ~0.027) |
| Max speakers | 4 (hard architectural cap) |
| Input | 16 kHz mono WAV / raw PCM16 LE |

## Why we layer on meet-parakeet-svc

The base image `meet-parakeet-svc:local` already ships CUDA 12.4 +
NeMo 2.4.1 + torch 2.4.1 + audio tooling. NeMo 2.4.1 includes
`SortformerEncLabelModel` out of the box, so no extra deps are
needed for v1. Reusing the base image means:

- build time on midboy2 is seconds (no second 5-8 GB CUDA stack),
- the `parakeet_models` named volume is shared so the HF cache is
  reused across parakeet-svc + parakeet-stream-svc + sortformer-svc,
- the NeMo behaviour matches the ASR services exactly so a torch /
  NeMo bug surfaces in all of them and we fix once.

## Endpoints

```
GET  /health             — liveness + model status + VRAM stats
GET  /readyz             — convention alias for /health
POST /diarize-stream     — per-window diarization (live path)
POST /diarize-file?audio_path=/abs/path  — file-shaped (finalize path)
```

### POST /diarize-stream

Request body: WAV bytes or raw PCM16 LE at 16 kHz mono.

Headers (all optional):

| Header | Meaning |
|---|---|
| `X-Session-Id` | opaque per-WS session identifier (logs only) |
| `X-Flush-Sequence` | per-session monotonic counter for log correlation |
| `X-Is-Final` | `1` if this is the last window of the segment |
| `X-Window-Start-Ms` | absolute ms-since-session-start of this window; if set, all `start_ms`/`end_ms` in the response are offset accordingly |

Response:

```json
{
  "speakers": [
    {"spk_id": 0, "spk_label": "speaker_0", "start_ms": 0,     "end_ms": 2560},
    {"spk_id": 0, "spk_label": "speaker_0", "start_ms": 3040,  "end_ms": 8720},
    {"spk_id": 1, "spk_label": "speaker_1", "start_ms": 15840, "end_ms": 21680}
  ],
  "window_end_ms": 33500,
  "is_final": false,
  "model": "nvidia/diar_sortformer_4spk-v1",
  "duration_seconds": 33.5,
  "rtf": 0.027,
  "distinct_speaker_count": 2,
  "sequence": null,
  "service_version": "0.1.0-spike"
}
```

### POST /diarize-file

For the finalize-audio worker which already has the session WAV
mounted in the container. Same response shape, no streaming
semantics. Up to 4 h audio by default.

## Build + run on midboy2

Build context is the contents of this directory. Pattern matches
parakeet-stream-svc — rsync to midboy2, build there.

```bash
# from a bigboy worktree:
rsync -av services/sortformer-svc/ \
  midboy2:/srv/uc-meeting-ops-deploy/midboy2/meet-sortformer-svc/

ssh midboy2 'cd /srv/uc-meeting-ops-deploy/midboy2/meet-sortformer-svc \
  && docker build -t meet-sortformer-svc:b3-spike .'

ssh midboy2 'docker run -d \
  --name meet-sortformer-svc \
  --restart unless-stopped \
  --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=0 \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
  -e LOG_LEVEL=INFO \
  -e SORTFORMER_PRECISION=fp16 \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -v parakeet_models:/models \
  -p 8896:8896 \
  meet-sortformer-svc:b3-spike'
```

Verify:

```bash
ssh midboy2 'curl -sS http://localhost:8896/health | python3 -m json.tool'
```

Expected (after ~30 s warm-up):

```json
{
  "status": "ok",
  "version": "0.1.0-spike",
  "model_loaded": true,
  "model": "nvidia/diar_sortformer_4spk-v1",
  "precision": "fp16",
  "phase": "B.3-spike",
  "vram_alloc_mib": 245,
  "vram_reserved_mib": 250,
  "max_speakers": 4
}
```

End-to-end probe with the speaker-svc 2-speaker fixture:

```bash
ssh midboy2 'docker cp meet-speaker-svc:/app/test_fixtures/synthetic_2speaker.wav /tmp/'
ssh midboy2 'curl -sS -X POST http://localhost:8896/diarize-stream \
  -H "Content-Type: audio/wav" \
  -H "X-Session-Id: probe" \
  --data-binary @/tmp/synthetic_2speaker.wav | python3 -m json.tool'
```

Expected: 2 distinct speaker IDs (spk_id=0 and spk_id=1), turns roughly
at 0-15 s and 15-33 s (af_bella + am_michael).

## Compose integration (not landed by spike)

The spike does NOT edit `docker-compose.midboy2.yml`. The recommended
additions are documented in `docs/phase-b3-sortformer-spike.md`
section "Compose additions" — they're a copy of the
parakeet-stream-svc block with the model env vars and port flipped.

## Networking

- Port **8896** on midboy2 host (88xx band, alongside `8881`
  parakeet-svc, `8889` speaker-svc, `8895` parakeet-stream-svc).
- The meet-backend on bigboy would reach it over Tailscale at
  `http://meet-sortformer-svc:8896` (configurable via `SORTFORMER_URL` env
  in the meet-backend container).
- No public ingress.

## VRAM budget on midboy2 GPU 0 (RTX 3060, 12 GB)

Measured during the spike with all four services warm and idle:

| Service | Model | Precision | Resident VRAM |
|---|---|---|---|
| meet-parakeet-svc | parakeet-tdt-1.1b | fp16 | ~4.0 GiB |
| meet-speaker-svc | wespeaker + community-1 | fp32 (CUDA) | ~2.0 GiB |
| meet-parakeet-stream-svc | parakeet-tdt-0.6b-v3 | fp16 | ~1.4 GiB |
| **meet-sortformer-svc** | diar_sortformer_4spk-v1 | fp16 | **~0.5 GiB** |
| **Total resident** | | | **~7.9 GiB / 12 GiB** |

~4.1 GiB headroom remaining. Inference peaks (1-2 concurrent
diarize windows + ASR window in flight) bring total to ~9-10 GiB.
Co-residency verdict: **fits with margin**. See spike report for
detailed measurement.

## Tests

```bash
# From inside this directory
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt fastapi pytest httpx python-multipart
pytest tests/ -v
```

The unit tests do not load the real Sortformer model — they
monkey-patch `_load_model` and `_diarize_wav_array` at the boundary
and exercise the HTTP surface only. End-to-end verification happens
out-of-process via the Docker healthcheck once deployed.

## References

- Design doc: [`docs/phase-b-server-live-streaming.md`](../../docs/phase-b-server-live-streaming.md)
- Spike report: [`docs/phase-b3-sortformer-spike.md`](../../docs/phase-b3-sortformer-spike.md)
- Sibling service (live ASR): `services/parakeet-stream-svc/`
- Sibling service (post-hoc diarization): `services/speaker-svc/`
- Upstream model card: <https://huggingface.co/nvidia/diar_sortformer_4spk-v1>
