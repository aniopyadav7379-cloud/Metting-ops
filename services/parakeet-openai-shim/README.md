# parakeet-openai-shim

A thin, **stateless** FastAPI service that puts an **OpenAI Whisper-compatible**
`POST /v1/audio/transcriptions` endpoint in front of the existing Parakeet STT
service (`parakeet-svc`). It also emits one **usage-metering** event per
successful transcription.

## Why this exists

The UC public inference gateway (LiteLLM) proxies OpenAI **audio** routes, so we
can serve STT "like an AI provider" alongside chat and embeddings. But
Parakeet's native API (`parakeet-svc`) is **not** OpenAI-shaped — it speaks a
UC-internal contract (`POST /transcribe`, multipart field `audio`, query params,
a richer JSON body). This shim translates between the two:

```
OpenAI client / UC gateway (LiteLLM)
    │  POST /v1/audio/transcriptions   (multipart: file, model, language, response_format, temperature)
    ▼
parakeet-openai-shim  ──►  POST {PARAKEET_URL}/transcribe   (multipart: audio; query: language, return_word_timestamps)
    │  OpenAI-shaped body (json | verbose_json | text)
    ▼  + 1 best-effort metering event  ──►  POST {METERED_REPORT_URL}
OpenAI client
```

It mirrors the exact request shape used by the backend's in-process Parakeet
provider (`backend/services/providers/impl_stt.py` → `LocalParakeetProvider`),
including the timeout scaling (`base 60s + 2s/MB`), so the on-the-wire contract
to `parakeet-svc` is identical whether STT is called in-process or through this
shim.

## The endpoint

### `POST /v1/audio/transcriptions`

Multipart form (OpenAI Whisper-compatible):

| Field             | Req | Behavior |
|-------------------|-----|----------|
| `file`            | yes | The audio upload. Forwarded to Parakeet as the `audio` field. |
| `model`           | no  | Accepted and **ignored** (e.g. `parakeet`, `whisper-1`). Parakeet picks its own model. |
| `language`        | no  | Forwarded to Parakeet (e.g. `en`). Defaults to `en`. |
| `response_format` | no  | `json` (default), `verbose_json`, or `text`. |
| `temperature`     | no  | Accepted and **ignored** (Parakeet has no sampling temperature). |

**Response format mapping:**

- `json` (default) → `{"text": "..."}`
- `verbose_json` →
  ```json
  {
    "task": "transcribe",
    "language": "en",
    "duration": 12.34,
    "text": "...",
    "segments": [
      {"id": 0, "seek": 0, "start": 0.0, "end": 3.1, "text": "...",
       "tokens": [], "temperature": 0.0, "avg_logprob": 0.0,
       "compression_ratio": 0.0, "no_speech_prob": 0.0}
    ],
    "words": [{"word": "...", "start": 0.0, "end": 0.4}]   // only if Parakeet returned them
  }
  ```
  Parakeet `segments` map to OpenAI segment objects (`id`, `seek:0`, `start`,
  `end`, `text` populated; the remaining OpenAI parity fields emitted as
  harmless defaults since Parakeet doesn't supply them). `return_word_timestamps`
  is requested from Parakeet **only** for `verbose_json`; the lighter formats
  skip the extra payload.
- `text` → raw transcript as `text/plain`.

**Errors** are returned in OpenAI shape — `{"error": {"message", "type", "code"}}`:

- Parakeet timeout → **504**
- Parakeet unreachable / non-JSON / 5xx → **502**
- Parakeet 4xx (e.g. "audio too long/short") → **400** (caller-fixable), message passed through
- Bad `response_format` / empty `file` → **400**

### `GET /health`

Liveness + effective config (no upstream call):

```json
{"ok": true, "status": "ok", "service": "parakeet-openai-shim", "version": "0.1.0",
 "parakeet_url": "...", "model_name": "parakeet", "metering_enabled": true, "org_header": "X-Org-Id"}
```

## Metering contract (LOCKED)

After a **successful** transcription, the shim emits **exactly one** usage event.
LiteLLM does **not** meter audio routes for us — the shim owns this.

```
POST {METERED_REPORT_URL}
Header: X-Metering-Key: {FEDERATION_KEY}
Body:   {"service_type": "stt", "org_id": "<org>", "quantity": <audio_seconds>}
```

- `quantity` = audio duration in **seconds**, taken from Parakeet's `duration`
  field. If Parakeet doesn't report a usable duration, `quantity` is **omitted**
  (we never send a bogus `0`).
- `org_id` comes from a request header (env `ORG_HEADER`, default `X-Org-Id`)
  injected by the gateway/caller. **If the org header is absent, metering is
  skipped** (a warning is logged) — STT still succeeds.
- Metering is **strictly best-effort and non-blocking**: it runs in a Starlette
  `BackgroundTask` *after* the response body is sent, with a short timeout
  (`METERING_TIMEOUT`, default 3s), wrapped in try/except. A metering failure or
  an ops-center outage **never** fails or delays a transcription.
- If `METERED_REPORT_URL` is unset, metering is disabled entirely (STT still
  works).

## Configuration (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `PARAKEET_URL` | `http://meet-parakeet-svc:8881` | parakeet-svc base URL. In-cluster: `http://meet-parakeet-svc:8881`. |
| `METERED_REPORT_URL` | *(empty → metering off)* | Full URL of the UC metering bridge. Internal: `http://ops-center:8084/api/v1/metered/report`. Public: `https://api.unicorncommander.ai/api/v1/metered/report`. |
| `FEDERATION_KEY` | *(empty)* | Value of the `X-Metering-Key` header on metering reports. |
| `ORG_HEADER` | `X-Org-Id` | Request header carrying the org id for usage attribution. |
| `SHIM_PORT` | `8011` | Port the shim listens on. |
| `STT_MODEL_NAME` | `parakeet` | Cosmetic model name (what the gateway advertises). |
| `PARAKEET_TIMEOUT_BASE` | `60.0` | Forward-timeout base seconds (mirrors `impl_stt.py`). |
| `PARAKEET_TIMEOUT_PER_MB` | `2.0` | Forward-timeout seconds per MB of upload. |
| `METERING_TIMEOUT` | `3.0` | Metering-call timeout (kept short on purpose). |
| `LOG_LEVEL` | `INFO` | Log level. |

## Run it (on midboy1 / bigboy, alongside parakeet-svc)

### Docker Compose (recommended)

`parakeet-svc` owns the GPU and the `meet-internal` network; this shim just
attaches to it. From this directory:

```bash
METERED_REPORT_URL=http://ops-center:8084/api/v1/metered/report \
FEDERATION_KEY=<federation-key> \
docker compose -f docker-compose.shim.yml up -d --build
```

The shim is published on `127.0.0.1:8011` (no public ingress — the gateway is
the only intended caller). It reaches parakeet-svc as `http://meet-parakeet-svc:8881`.

### Plain `docker run`

```bash
docker build -t meet-parakeet-openai-shim:local .

docker run -d --name meet-parakeet-openai-shim \
  --network meet-internal \
  -p 127.0.0.1:8011:8011 \
  -e PARAKEET_URL=http://meet-parakeet-svc:8881 \
  -e METERED_REPORT_URL=http://ops-center:8084/api/v1/metered/report \
  -e FEDERATION_KEY=<federation-key> \
  meet-parakeet-openai-shim:local
```

### Smoke test

```bash
curl -s http://127.0.0.1:8011/health | jq .

# json (default)
curl -s http://127.0.0.1:8011/v1/audio/transcriptions \
  -H 'X-Org-Id: org_demo' \
  -F file=@meeting.wav -F model=parakeet -F language=en | jq .

# verbose_json (asks Parakeet for word timestamps)
curl -s http://127.0.0.1:8011/v1/audio/transcriptions \
  -H 'X-Org-Id: org_demo' \
  -F file=@meeting.wav -F response_format=verbose_json | jq '.segments | length'

# text
curl -s http://127.0.0.1:8011/v1/audio/transcriptions \
  -F file=@meeting.wav -F response_format=text
```

## Integration points (OPEN — for the gateway/suite dev)

This shim deliberately leaves the auth/tenancy/routing wiring to the gateway. To
wire it into the UC public inference gateway:

1. **Route the OpenAI audio path to this shim.** Point LiteLLM's
   `/v1/audio/transcriptions` (the model you advertise as `parakeet` /
   `whisper-1`) at `http://<host>:8011/v1/audio/transcriptions`. The shim is the
   provider backend for STT; LiteLLM should **not** also meter this route
   (metering is owned here — see below).

2. **Inject the org header.** The shim attributes usage from a request header
   (`ORG_HEADER`, default `X-Org-Id`). The gateway must resolve the caller's org
   (from their API key / token) and inject it as `X-Org-Id: <org>` on the
   forwarded request. If it's absent the shim still transcribes but **skips
   metering** (logged) — so don't rely on the shim to enforce tenancy; that's
   the gateway's job. (Auth/rate-limit/tenancy all live at the gateway; the shim
   is intentionally unauthenticated and internal-only.)

3. **Set the metering destination + key.** Provide `METERED_REPORT_URL` (the UC
   metering bridge — internal `http://ops-center:8084/api/v1/metered/report` for
   on-node, or public `https://api.unicorncommander.ai/api/v1/metered/report`)
   and `FEDERATION_KEY` (sent as `X-Metering-Key`). The shim emits
   `{"service_type":"stt","org_id":...,"quantity":<seconds>}`. Confirm the
   bridge's expected `service_type` token (`stt`) and that `quantity` in
   **seconds** is the unit the Lago metric expects; this is the one knob to
   reconcile against the non-LLM metering rail.

4. **Network reachability.** The shim only needs to reach `PARAKEET_URL` and
   `METERED_REPORT_URL`. Keep it internal/localhost-bound; expose it to the
   gateway over the shared docker network or a Tailscale/SSH hop, never publicly.

Nothing in the shim is stateful — scale it with plain replicas behind the
gateway if STT fan-out grows (parakeet-svc itself still serializes on one GPU).
