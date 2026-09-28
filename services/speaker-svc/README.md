# speaker-svc

Internal microservice for **speaker embedding + diarization**, deployed alongside the
Meeting-Ops backend on bigboy GPU 1.

## Models

| Model | Source | Required token? | What it does |
|---|---|---|---|
| `speechbrain/spkrec-ecapa-voxceleb` | open weights, baked into the image | no | 192-d speaker embeddings (`/embed`, `/identify`) |
| `pyannote/speaker-diarization-3.1` | gated on Hugging Face | **yes** (`HUGGINGFACE_TOKEN`) | turn-level diarization (`/diarize`) |

If `HUGGINGFACE_TOKEN` is not set, `/diarize` gracefully degrades to a single
ECAPA segment — admins can still enroll and identify speakers, just without
turn-level diarization.

## Build + run on bigboy

```bash
cd /srv/meeting-ops/services/speaker-svc
docker build -t meet-speaker-svc:local .

docker run -d --name meet-speaker-svc \
  --gpus '"device=1"' \
  --network meet-internal \
  -e HUGGINGFACE_TOKEN=hf_xxx \
  -v /opt/speaker-models:/models \
  -p 127.0.0.1:8889:8889 \
  meet-speaker-svc:local
```

The Meeting-Ops backend reaches it as `http://meet-speaker-svc:8889` over the
internal network. There is **no** public ingress for this service.

## Enabling Pyannote 3.1 (admin one-time setup)

1. Visit https://huggingface.co/pyannote/speaker-diarization-3.1 and accept the terms.
2. Visit https://huggingface.co/pyannote/segmentation-3.0 and accept the terms (it's a dependency).
3. Mint a token at https://huggingface.co/settings/tokens (read scope is enough).
4. Set `HUGGINGFACE_TOKEN=hf_xxx` in the speaker-svc env (e.g. in `.env.bigboy`) and restart.
5. Verify: `curl http://meet-speaker-svc:8889/health` should now show
   `"diarizer_available": true` and `"diarizer_error": null`.

The token is **only** used by speaker-svc — the embeddings/RAG service uses
`INFINITY_ENDPOINT` and is unrelated.

## API quick reference

```bash
# Embed a clip
curl -F audio=@speaker_clip.wav http://meet-speaker-svc:8889/embed | jq .embedding_dim
# -> 192

# Diarize a meeting
curl -F audio=@meeting.wav http://meet-speaker-svc:8889/diarize | jq .num_speakers

# Score a query embedding against enrolled speakers
curl -X POST -H 'Content-Type: application/json' http://meet-speaker-svc:8889/identify \
  -d '{"embedding":[...192 floats...],"candidates":[{"speaker_id":1,"embedding":[...]}],"threshold":0.55}'
```

All embeddings are 192-d float32 and ship as JSON arrays. The DB column is
`embedding_dim INT` + `embedding BYTEA` so swapping in a wider model later
(e.g. ECAPA-XL at 256-d, or WavLM at 768-d) is a config change, not a schema change.

## Health probes

| Endpoint | What it checks | Used by |
|---|---|---|
| `GET /health` | HTTP server is up, reports cuda + model-load status | bootstrap / quick liveness checks |
| `GET /healthz/synthetic` | Full diarization pipeline: loads bundled 2-speaker WAV, runs it through `_run_diarization_on_wav()`, asserts `num_speakers == 2`, segments > 1, and cosine distance between cluster centroids >= 0.4 | Docker healthcheck + on-call alerting |

The synthetic probe (task #105) catches the pipeline-state-degradation class
of failure where pyannote returns collapsed clusters (everyone-as-one-speaker)
and only a container restart at the same clustering threshold fixes it. The
plain `/health` is HTTP-up-only and can't see this.

Test fixture lives at `test_fixtures/synthetic_2speaker.wav` with a sibling
`.meta.json` describing ground truth. Bundled inside the image, no network
required at probe time.

Sample passing response:

```json
{
  "status": "ok",
  "speaker_count": 2,
  "segments": 3,
  "elapsed_ms": 412,
  "fixture": "synthetic_2speaker.wav",
  "backend": "pyannote-community-1",
  "embedding_distance": 0.9772,
  "embedding_distance_required": 0.4,
  "checked_at": "2026-05-22T07:00:00Z"
}
```

Sample degraded response:

```json
{
  "status": "degraded",
  "reason": "speaker_count_mismatch",
  "expected": 2,
  "speaker_count_actual": 1,
  "segments": 1,
  "fixture": "synthetic_2speaker.wav",
  "backend": "pyannote-community-1",
  "elapsed_ms": 380,
  "checked_at": "2026-05-22T07:05:00Z"
}
```

Possible `reason` values: `fixture_missing`, `fixture_unreadable`,
`diarization_threw`, `speaker_count_mismatch`, `segments_too_few`,
`embedding_distance_too_low`.

## Tests

```bash
# From inside this directory
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt fastapi pytest httpx python-multipart
pytest tests/ -v
```

The unit tests do not touch the real pyannote models — they monkey-patch
`_run_diarization_on_wav` at the boundary and exercise the probe's branching.
End-to-end probe verification happens out-of-process via the Docker
healthcheck once the container is deployed.
