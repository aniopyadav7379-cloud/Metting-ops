# Meeting-Ops Recording Appliance
Part of the Unicorn Commander Suite

## Project Overview
AI-powered meeting recording and transcription: browser-first live capture, real-time transcription, speaker diarization, and meeting insights, with a server completion pass on the bigboy GPU cluster (Parakeet 1.1B STT, pyannote 3.1 diarization, Qwen 3.6 35B-A3B-Vision summaries). Cloud and on-prem/appliance builds ship from the same codebase via compose targets.

**Status**: Backend live | Frontend live | Auth working | LLM integrated | Mobile responsive | browser-first capture with server completion pass | Knowledge Graph (person-centric) LIVE

**Latest release**: v3.57.3 — docs/README refresh to v3.57.x reality + human manual-test-plan (docs/manual-test-plan.md) + local-only-mode doc; retired generic-mic brand png removed. Prior: v3.57.2 — reveal-safety for captures/print (`?static=1`). Prior: v3.57.1 — hero officer-unicorn watermark + branded static pre-mount shell (no-JS/pre-JS HTML now carries tagline, pitch, CTAs; signed-in users skip the marketing flash). Prior: v3.57.0 — **Chunked local full pass + session-lifetime follow-up wave**: the local-only stop-time Parakeet pass runs in ~5-min silence-aligned windows with a yielding mel loop and live progress (no more frozen laptops; sub-8GB cap doubles to ~1h; windows stay inside the model's ~24-min envelope so long transcripts get *more* accurate), the knowledge-graph endpoint joins the canonical cross-org session resolver (last strict-org holdout), tab/SERP title settled to "Meetings become memory, decisions, and work", dogfood proxy cookie 24h→168h to match the new Keycloak realm policy (3d idle / 30d max on both uchub realms), and both stacks' expired `STRIPE_API_KEY` replaced (annual price mapping verified correct: $150/yr). Prior: v3.56.0 — **Diarization re-detect + session-page polish**: re-detect can now *reduce* the speaker count (merge path replaced stale pyannote turns instead of relabeling them), rediarize no longer loses cross-org sessions, Auto re-detect sends plain auto-detect (speaker-svc rejects per-request thresholds), duration formatters floor fractional seconds, 12 invisible white-on-white card headers fixed, session pages open at the top, and the live-summary panel explains what it actually does. Prior: v3.55.0 **OC entitlement login sync**: Meeting-Ops now honors Ops-Center central Meeting-Ops grants at Unicorn Commander SSO login, in a dormant fail-open path that upgrades local Pro only when OC returns a paid tier plus `meeting_ops_access`. Prior: v3.54.0 Launch Console invite-code emailing and cohort distribution; v3.53.0 comps unlock Pro on BOTH gated surfaces (user tier + org plan) + 100+100 launch codes; v3.50.0 in-app "Upgrade to Pro" Stripe checkout + $15 launch pricing, concurrent upload STT‖diarize, summarizer gateway-auth (401) fix, and a summary re-drive watchdog (see `CHANGELOG.md`). Prior milestone v3.40.0 — enterprise/SaaS production-readiness hardening. The v3.40.0 arc closed a multi-agent audit (59 findings): the critical SSO forward-auth trust boundary (`X-Proxy-Auth`), per-workspace billing (paid server-compute keys off the **active org's** plan, not just the global user tier), fail-closed `SECRET_KEY` boot guard + no-double-bill, HTTP security headers + WebSocket handshake auth (`WS_REQUIRE_AUTH` kill-switch), upload recovery + deep readiness probes + Arq retries, Sentry + error boundaries + HTTP metrics, cursor pagination + a dedicated interactive worker lane, session-delete erasure (Qdrant + chat history) + per-room retention opt-in (`Room.retention_enabled`, migration 049). Earlier milestone v3.30.0 — production-ready / pre-launch (v3.29.0 → v3.30.0): a multi-agent production-readiness + security pass (locked the unauth `websocket_auto_summary`, WS handshake auth, Brigade tenancy **fail-closed**, `/api/system-info` auth, tier-gated server-compute paths); the Knowledge Graph went GA (person-centric, hydrated) and cross-meeting search/RAG were made whole (reprocess indexes to Qdrant, all meetings backfilled); embeddings + reranking moved onto the shared Infinity server; the Arq reprocess pipeline was repaired + hardened (heartbeat watchdog, env-parity tests); Project-Ops / Contact-Ops federation wired; **session cards** show diarized named speakers; a Sessions-list **N+1 perf fix** + per-page control + a frictionless inline **speaker-naming** prompt; and a **pre-onboarding sweep** that made every Settings panel honest (removed a fake "NPU / 220×" banner, corrected stale model names everywhere, AI settings → a live read-only engine panel) plus stale-bundle auto-recovery.

**The live inference stack** (what actually runs — NOT Whisper/Granite/gpt-oss, which were stale legacy labels corrected in the sweep): STT = **Parakeet** (in-browser 0.6B live + server **Parakeet 1.1B** completion pass); diarization = **pyannote 3.1**; LLM (summaries / titles / RAG-chat / vocal-script) = **Qwen 3.6 35B-A3B-Vision** (MoE) via LiteLLM, fast model **gemma-4-e4b**; embeddings + reranking = **Infinity** (bge-m3 / bge-reranker-v2-m3); TTS = **Kokoro** (af_heart). The codebase is browser-first by design (below); the server runs only the per-meeting completion pass.

## Architecture is browser-first by design

Live STT and live summarization run in the user's browser (in-browser Parakeet for live transcription + a small on-device summary LLM via onnxruntime-web / transformers.js / WebGPU, cached in IndexedDB). The server only runs at meeting completion — a single per-meeting pass that does the canonical Parakeet 1.1B transcription, pyannote 3.1 diarization (via the `meet-speaker-svc` service), and a Qwen 3.6 35B-A3B-Vision summary/title — then indexes the result for cross-meeting search. Privacy mode skips the server pass entirely; nothing leaves the device.

This isn't an aesthetic choice — it's the moat. Our marginal cost per concurrent user-hour is $0 in compute (the user's CPU/GPU does it). Competitors pay AssemblyAI / Deepgram per audio-minute and OpenAI / Anthropic per token, per user, continuously. At scale we're 10-100x cheaper to run.

**Default new features to browser-side compute.** Only fold a feature into the server completion pass if quality genuinely requires it. Only reach for a third-party API as a last resort, behind a feature flag, with the cost owner explicit. See `docs/compute-economics.md` for the full strategy doc + the decision tree for future architecture choices.

## Design docs

Canonical index: `docs/README.md`. It groups every document in `docs/` by topic, includes line counts, and records shipped vs designed status.

- `docs/compute-economics.md`: Browser-first compute model, pricing tiers, cost moat, and deployment topology.
- `docs/POSTGRESQL_QDRANT_SETUP.md`: PostgreSQL plus Qdrant setup, SQLite migration, and semantic search operations.
- `docs/WYOMING_PROTOCOL.md`: Wyoming Protocol reference for Home Assistant satellites, wake words, gestures, and remote activation.
- `docs/always-on-recording-design.md`: Earlier continuous recording and silence-gap segmentation design.
- `docs/companion-app-design.md`: Desktop companion app design for mic plus system-audio capture.
- `docs/satellite-devices-design.md`: ESP32-S3 and Raspberry Pi satellite-device architecture.
- `docs/phase-b-server-live-streaming.md`: Paid-tier server-live streaming design with Parakeet streaming and Qwen rolling summaries.
- `docs/phase-c1-native-ios-design.md`: Native iOS, Core ML, SwiftUI, watchOS, FluidAudio, and Apple Foundation Models plan.
- `docs/bulk-audio-import-design.md`: Bulk import pipeline for the 526-file backlog and `/import` workflow.
- `docs/conference-room-design.md`: Conference Room mode, room entities, USB mic path, and satellite convergence.
- `docs/brigade-integration-design.md`: Meeting-Ops to Brigade graph writer and 3D viewer integration.
- `docs/appliance-extraction-design.md`: Appliance extraction into a separate enterprise on-prem repo via submodule.
- `docs/screen-recording-design.md`: Personal screen plus audio recording MVP design.
- `docs/image-registry-decision.md`: Forgejo container registry decision for canonical image distribution.

## Active roadmap

Mobile and native roadmap is locked as A → B → C-1 → C-2 → C-3:

1. Phase A, mobile PWA capture-only: SHIPPED v0.8.0 + v0.8.1. Mobile captures + uploads with completion processing after stop.
2. Phase B, server-live streaming: SHIPPED through v2.1.0. Sortformer 4-speaker live diarization, Parakeet realtime-EOU 120M streaming, per-word `tokens_finalized` + EOU detection, utterance-grouped UI in the Record page. Gated on `hasFeature('server_live')` (enterprise/pro/superuser). See `docs/runbook-streaming-v2.md` for operational details. Remaining: Opus uplink (utility shipped v1.2.0 but capture path not wired), full deprecation of v1.3.0 cursor+VAD workarounds (cheap insurance for now).
3. Phase C-1, native iOS: C-1.2b SHIPPED — `MeetingOpsCore` is now wired into the Xcode app target and the iOS app target **builds sim-green** (independently verified: `xcodebuild` BUILD SUCCEEDED + `swift test` 14/0). Done: C-1.1 scaffold (auth/REST/WS/AVAudioEngine) + C-1.2 (FluidAudio/Parakeet on-device STT, GRDB, BackgroundAudioUploader, SessionViewModel) + C-1.2b (target wire-up + PCM16→AVAudioPCMBuffer bridge fix). Repo `UC-Meeting-Ops-iOS` on Forgejo, branch `phase-c1-2b-xcode-wireup @ da92313`. NEXT: real-device smoke on iPhone 14 Pro Max (A16) → C-1.3 on-device summary (mlx-swift Qwen/Gemma; the A16 can't run Apple Foundation Models) → GUI/UX pass. Tracked in Project-Ops P-00064; see repo docs `C-1.2b-STATUS.md` / `C-1.2b-DEVICE-TEST.md` / `iOS-GUI-BRIEF.md`.
4. Phase C-2, watchOS extension on top of C-1: future.
5. Phase C-3, native Android plus NNAPI: future.

Queued behind product/design decisions:
- Sortformer canonical /finalize-audio: opt-in hybrid SHIPPED v2.2.0 but PARKED — Sortformer v1 one-shot diarize OOMs on real-length meetings (38-min needs ~25 GiB on the 12 GB 3060). pyannote stays canonical. Promotion needs chunk+embedding-stitch, the streaming-state API, or NeMo 2.5. See CHANGELOG 2.2.0 + runbook.
- Free-tier: SHIPPED — enforcement v3.0.0 (server endpoints 403 free users) + wiring v3.1.0 (frontend auto-routes free users to on-device/local-only, upgrade prompts, Local Sessions nav). gate helpers in auth/tier.py; useTierFeatures in frontend. Consumer signup (Option B) backend SHIPPED v3.2.0 (inert; see docs/consumer-signup.md for activation). Remaining before free-tier LAUNCH: on-device transcript-quality validation (voice test), signup activation + frontend /signup+/login+/pricing pages, password-reset endpoints.
- PWA deploy coordination: SHIPPED v2.3.0 — SW switched to prompt/parked (skipWaiting false); update banner deferred while recording (live or always-on), surfaces on stop.
- Pricing decision + Stripe checkout build

## Quick Start

```bash
# Start Docker services (PostgreSQL, Redis, Qdrant; LLM is reached via LiteLLM)
docker compose -f docker-compose-full-stack.yml up -d

# Start backend (port 9050)
cd backend && ./start-backend.sh

# Start frontend (port 7777)
cd frontend && npm run dev -- --host 0.0.0.0

# Run tests
cd backend && python3 -m pytest tests/ -v
cd frontend && npm run build
cd frontend && npx vitest run
```

**Login**: admin / admin123 (superuser)

**Access**:
- Frontend: http://your-server-ip:7777
- Backend API docs: http://your-server-ip:9050/docs

## Architecture

### LLM: Qwen 3.6 35B-A3B-Vision via llama.cpp, fronted by LiteLLM
- Default model: Qwen 3.6 35B-A3B-Vision — a Mixture-of-Experts model (~3B active params) served via llama.cpp and exposed through LiteLLM at `unicorn-litellm:4000` (OpenAI-compatible)
- Fast model: `gemma-4-e4b` (used for cheap/low-latency calls)
- Vision-capable (handles OCR / image-bearing context)
- API: OpenAI-compatible `/v1/chat/completions` against the LiteLLM gateway
- Used for: summaries, titles, AI chat, insights, sentiment analysis, vocal-summary narration
- Model registry: `backend/config/` — centralized model configs; active model persisted in settings

### Frontend (React 19 + TypeScript + Vite + TailwindCSS)
Active router: `AppRouterSimplified.tsx` (mobile-responsive with hamburger menu)

| Route | Page | Purpose |
|-------|------|---------|
| /login | Login.tsx | Authentication |
| /record | LiveRecording.tsx | Recording + live transcription |
| /sessions | Sessions.tsx | Session list + search + pagination |
| /sessions/:id | SessionDetails.tsx | Playback + transcript + export + AI chat |
| /settings | SettingsEnhanced.tsx | System settings + vocabulary |
| /admin/agents | AgentDashboard.tsx | AI agent management |
| /admin/agents-old | AgentConfiguration.tsx | Legacy agent config |

Contexts: AuthContext, ThemeContext, RecordingContext
Shared components: Login, ErrorMessage, LoadingSpinner, RecordingIndicator, SessionCreator, Toast

### Backend (FastAPI + Python 3.13)

**main.py** loads the routers in `backend/api/` with status tracking:
- A small set are required (auth, recording, sessions) - fail fast
- The rest are optional (agent, AI, analytics, satellite, etc.) - log warning on failure
- `/health` reports router status and real timestamps

**Key API routers** (in `backend/api/`):
- `simple_recording_db.py` - Recording CRUD + audio capture + full-text search
- `sessions.py` - Session management
- `ai_chat.py` - Per-meeting AI chat (real LLM)
- `ai_insights.py` - AI-powered insights (keywords, sentiment, action items via _call_llm)
- `ai_settings.py` - Model list/switch endpoints, AI settings
- `analytics_simple.py` - Speaker analytics + duration trends
- `batch_export.py` - PDF/DOCX/TXT/JSON/SRT export
- `vocabulary.py` - Custom vocabulary CRUD
- `websocket_transcription.py` - Live transcription via WebSocket
- `websocket_auto_summary.py` - Progressive AI summaries
- `satellite_api.py` - Satellite device CRUD, heartbeat, audio/transcript upload
- `websocket_satellite.py` - Real-time audio streaming from satellite devices
- `websocket_remote_audio.py` - Companion app remote audio WebSocket

**Key services** (in `backend/services/`):
- `unified_llm_service.py` - LLM via the LiteLLM OpenAI-compatible gateway (Qwen 3.6 35B-A3B-Vision)
- `unified_agent_service.py` - Progressive / completion-pass summarization
- `working_audio_service.py` - USB mic audio capture
- `transcription_service.py` - Transcription orchestration (server completion pass → Parakeet 1.1B)

### Server-side AI services (server completion pass)
- **STT**: Parakeet 1.1B for the server completion pass; in-browser Parakeet for the live transcript
- **Diarization**: pyannote 3.1 via the `meet-speaker-svc` service (runs on bigboy's RTX 3090)
- **LLM**: Qwen 3.6 35B-A3B-Vision (MoE, ~3B active) via llama.cpp behind LiteLLM at `unicorn-litellm:4000`; fast model `gemma-4-e4b`
- **Embeddings + reranking**: shared Infinity server — `BAAI/bge-m3` (1024-dim dense) + `bge-reranker-v2-m3`; sparse BM25 stays local
- **TTS**: Kokoro (`af_heart` voice) for vocal summaries

### Infrastructure
- PostgreSQL (session/transcript storage)
- Redis (real-time pub/sub + Arq reprocess queue)
- Qdrant — collection `meet_transcripts`, hybrid vector search (dense + BM25 sparse)
- LiteLLM gateway (`unicorn-litellm:4000`) fronting the llama.cpp LLM
- Shared Infinity server for embeddings + reranking

## API Endpoints

```
POST /api/auth/login                              # Login (admin/admin123)
POST /api/simple/recording-sessions               # Create session
POST /api/simple/recording-sessions/{id}/start    # Start recording
POST /api/simple/recording-sessions/{id}/stop     # Stop recording
GET  /api/simple/recording-sessions               # List sessions
GET  /api/simple/recording-sessions/{id}          # Get session + transcript
GET  /api/simple/recording-sessions/search?q=...  # Full-text search
GET  /api/simple/recording-sessions/{id}/download/audio   # Download WAV
GET  /api/simple/recording-sessions/{id}/download/summary/pdf   # PDF export
GET  /api/simple/recording-sessions/{id}/download/summary/docx  # DOCX export
POST /api/ai-chat/sessions/{id}/messages          # AI chat with meeting context
POST /api/ai-chat/rag/query                       # Cross-meeting RAG query (multi-turn)
GET  /api/ai-chat/rag/history                     # RAG chat history
GET  /api/simple/recording-sessions/semantic-search?q=...  # Hybrid semantic search (Qdrant)
GET  /api/analytics/summary                       # Analytics with speaker data
GET  /api/analytics/speakers                      # Speaker analytics
GET  /api/analytics/duration-trends               # Duration trends
GET  /api/simple/recording-sessions/{id}/insights  # AI insights (keywords, sentiment)
WS   /ws/transcription/{session_id}               # Live transcription
WS   /ws/audio-levels                             # Audio level monitoring
GET  /api/settings/models                          # Model info + status
GET  /api/settings/models/available                # List all models with active indicator
POST /api/settings/models/active                   # Switch active LLM model
GET  /health                                       # Health + router status
# Satellite Devices
POST /api/satellites/register                      # Register satellite device
GET  /api/satellites                               # List all satellites
GET  /api/satellites/{device_id}                   # Get satellite details
PUT  /api/satellites/{device_id}                   # Update satellite config
DELETE /api/satellites/{device_id}                  # Remove satellite
POST /api/satellites/{device_id}/heartbeat         # Device heartbeat
POST /api/satellites/{device_id}/upload-audio       # Upload WAV (store-and-forward)
POST /api/satellites/{device_id}/transcript         # Upload transcript (local STT)
POST /api/satellites/{device_id}/start-recording    # Trigger recording
POST /api/satellites/{device_id}/stop-recording     # Stop recording
GET  /api/satellites/rooms                          # List rooms with satellites
WS   /ws/satellite/{device_id}/audio               # Real-time satellite audio stream
# Companion App
WS   /ws/remote-audio/{session_id}                  # Remote audio stream from companion app
```

### MCP Server (Model Context Protocol)
Standalone MCP server for external AI agent access to Meeting-Ops data.
- Location: `mcp/meeting_ops_mcp.py`
- Transport: stdio (for Claude Desktop, Open WebUI, etc.)
- 8 tools: search_meetings, ask_about_meetings, list_meetings, get_meeting_details, get_meeting_transcript, chat_with_meeting, get_analytics, get_meeting_insights
- 2 resources: meetings://list, meetings://{session_id}
- 2 prompts: meeting_analysis, cross_meeting_research
- Config: `MEETING_OPS_URL`, `MEETING_OPS_USER`, `MEETING_OPS_PASS` env vars

```bash
# Run standalone
python3 mcp/meeting_ops_mcp.py

# Claude Desktop config (~/.claude/claude_desktop_config.json)
# { "mcpServers": { "meeting-ops": { "command": "python3", "args": ["/path/to/mcp/meeting_ops_mcp.py"] } } }
```

## File Structure
```
UC-Meeting-Ops/
  backend/
    main.py                    # FastAPI app, loads the routers in api/
    api/                       # API routers (incl. satellite, remote audio)
    services/                  # service modules
    auth/                      # JWT authentication + tier gating
    database/                  # SQLAlchemy models + connection
    models/                    # Unified agent + vocabulary models
    config/                    # AI backend config + model registry
    workers/                   # Arq reprocess / bulk-import workers
    tests/                     # backend pytest suite
    start-backend.sh           # Start script
  frontend/
    src/
      App.tsx                  # Root component (+ ToastContainer)
      AppRouterSimplified.tsx  # Router (mobile responsive)
      pages/                   # page components
      components/              # shared components
      contexts/                # context providers
      hooks/                   # hooks
      __tests__/               # frontend vitest suite
      config.ts                # API URL configuration
  mcp/
    meeting_ops_mcp.py         # MCP server for external AI agents
  services/
    speaker-svc/               # meet-speaker-svc: pyannote 3.1 diarization + embeddings
  docs/                        # Design documents
  docker-compose-full-stack.yml
```

## Cloud deployment (bigboy)

The cloud Meeting-Ops instance lives at `/srv/meeting-ops/` on bigboy (private network). Source tree is at `/srv/meeting-ops/src/` (where .git lives).

### Compose invocation quirk

`deploy/` is symlinked from the project root: `/srv/meeting-ops/deploy → src/deploy`. The compose file uses a build context of `../../src/backend` which only resolves via the symlinked path. Therefore:

```bash
# CORRECT — run from project root (where the symlink is)
cd /srv/meeting-ops/
docker compose --env-file deploy/bigboy/.env.bigboy -f deploy/bigboy/docker-compose.bigboy.yml up -d --force-recreate --build

# WRONG — runs from src/, build context resolves to a non-existent path
cd /srv/meeting-ops/src/
docker compose -f deploy/bigboy/docker-compose.bigboy.yml ...
```

Always invoke compose from `/srv/meeting-ops/`, not from `/src/`.

### Always pass --env-file

Compose without `--env-file deploy/bigboy/.env.bigboy` will silently fall back to a partial environment and may boot containers with wrong config. The env file is required — make it part of every `docker compose` invocation.

## Versioning + CHANGELOG

This project uses [Semantic Versioning](https://semver.org/). Current series: v0.8.x.

`CHANGELOG.md` at repo root tracks every release in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. Update CHANGELOG.md alongside `frontend/package.json` version bump on every release.

Workflow:
1. Make changes, commit normally.
2. When tagging a release: bump `frontend/package.json` version, update CHANGELOG.md (move `[Unreleased]` content to the new version section with date).
3. Annotated tag with the same release notes as the CHANGELOG entry: `git tag -a vX.Y.Z -m '<release notes>'`.
4. Push branch + tag: `git push origin main && git push origin vX.Y.Z`.

Tags ship to canonical Forgejo: `git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops`.

## Inference / GPU routing (verified 2026-07-28)

Canonical map: **`/srv/uc-cloud/docs/gpu-fleet-map.md`** (bigboy).
Read it before assuming which card runs what — several aliases are misleading.

**Priority routing, not load-balancing.** litellm runs `routing_strategy: least-busy`,
which **ignores `weight:`**. Priority is expressed as a *single-member pool* plus
`litellm_settings.fallbacks`:

- **Long context (131k)** — `qwen3.6-35b-a3b` / `-64k` / `-long`
  → bigboy **3090** → `qwen3.6-35b-a3b-ha` (legacy1 P40 :8091, 131k) → cloud
- **Batch (32k × 3)** — `qwen3.6-35b`
  → **legacy1** card1 :8092 → `qwen3.6-35b-p40` (midboy2) → cloud

⚠️ **Alias traps**
- `qwen3.6-35b-p40` is midboy2's **32k × 3** node, NOT long-context. The 131k P40 is `qwen3.6-35b-a3b-ha`.
- `qwen3.6-35b-midboy1` actually points at **bigboy's** RTX 6000.
- "64k" in `qwen3.6-35b-64k` is a legacy name; both members are 131k-capable.

⚠️ **Verify advertised context against reality.** The gateway was over-advertising
`model_info.max_tokens` (e.g. `qwen3.6-35b-p40` claimed 131072 while serving 32768),
so callers sized prompts off a lie and silently overflowed. Audit with:
`GET :4000/v1/model/info` → `max_tokens` vs each backend's `GET <api_base>/props`
→ `n_ctx` ÷ parallel count. A deployment with **no** `max_tokens` inherits and is
just as dangerous as a wrong one.
