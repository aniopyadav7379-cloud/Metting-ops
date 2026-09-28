# vibevoice-svc

Long-form, multi-speaker TTS for UC Meeting-Ops. Wraps Microsoft VibeVoice
(October 2024) behind a small FastAPI surface so the rest of the stack can
treat it like any other provider.

## Why

Kokoro is fast and small but tops out around a paragraph or two and stays
single-voice. VibeVoice is purpose-built for podcast-style audio: 90+ minutes
of synthesis with native multi-speaker turn-taking. Meeting-Ops uses VibeVoice
to render a meeting summary as a podcast recap (host + analyst).

## Where this lives

- Source of truth: `src/services/vibevoice-svc/` in the UC-Meeting-Ops repo.
- Runtime: `meet-vibevoice` container on **midboy1** GPU 1 (Tesla P40).
- Backend client: `backend/services/providers/impl_tts.py::VibeVoiceProvider`.

The midboy1 box is on a different docker network than bigboy, so the backend
calls VibeVoice via the LAN IP (`http://<infinity-host>:8882`), wired up in
`deploy/bigboy/.env.bigboy::VIBEVOICE_ENDPOINT`.

## Endpoints

| Method | Path        | Body                                                        | Returns                                  |
|--------|-------------|-------------------------------------------------------------|------------------------------------------|
| GET    | `/health`   | —                                                           | `{status, model_loaded, gpu_name, ...}`  |
| GET    | `/voices`   | —                                                           | `{voices: [{voice_id, path}, ...]}`       |
| POST   | `/tts`      | `{text, voice_id, format}`                                  | audio/wav or audio/mpeg                   |
| POST   | `/podcast`  | `{script: [{speaker_id, text}], voices: {speaker_id: voice_id}, format}` | audio/wav or audio/mpeg                   |

`format` is `mp3` or `wav`. `cfg_scale` is optional (default 1.3).

## Build / run

```bash
# Build the image
docker build -t meet-vibevoice:local .

# Run (P40 device 1)
docker run -d \
  --name meet-vibevoice \
  --runtime nvidia \
  --gpus '"device=1"' \
  -p 8882:8882 \
  -v /opt/vibevoice-models:/models \
  meet-vibevoice:local

# Health
curl -s http://localhost:8882/health
```

The Dockerfile clones `microsoft/VibeVoice` and installs it in-place because
PyPI does not (yet) host an official wheel.

## Smoke test

```bash
curl -s -o /tmp/hello.mp3 -X POST http://localhost:8882/tts \
  -H 'content-type: application/json' \
  -d '{"text":"Hello from VibeVoice.","voice_id":"alice","format":"mp3"}'
file /tmp/hello.mp3
```

## Caveats

- VibeVoice-1.5B uses ~6-8 GB VRAM steady-state. The 7B variant is overkill for
  this use case and pulls in extra HF gated weights.
- First request after a cold start can take ~30s while the model is materialised
  on the GPU. Eager-load is on by default; flip `VIBEVOICE_EAGER_LOAD=false` to
  defer loading until first use.
- The LAN endpoint is open inside our VLAN. Anything reaching the public web
  should still go through oauth2-proxy on bigboy.
