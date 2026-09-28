# Meeting-Ops Backend Development Guide

## Quick Start
```bash
cd backend
./start-backend.sh  # Starts on port 9050

# Or manually:
uvicorn main:app --host 0.0.0.0 --port 9050 --reload

# Database (PostgreSQL via Docker)
docker compose -f docker-compose-full-stack.yml up -d

# LLM is reached over HTTP via the LiteLLM gateway (OpenAI-compatible)
# Qwen 3.6 35B-A3B-Vision behind llama.cpp, fronted by LiteLLM at unicorn-litellm:4000
```

## Architecture

### LLM Integration
- Default model: Qwen 3.6 35B-A3B-Vision — Mixture-of-Experts (~3B active params), served via llama.cpp
- Gateway: LiteLLM at `unicorn-litellm:4000` (OpenAI-compatible `/v1/chat/completions`)
- Fast model: `gemma-4-e4b` for cheap / low-latency calls
- Vision-capable (OCR / image-bearing context)
- Model registry: `config/` — centralized configs; active model persisted in settings
- Service: `services/unified_llm_service.py` (singleton, `llm_service` global)
- Used by: ai_chat, ai_insights, unified_agent_service, meeting_notes_service, vocal-summary narration

### main.py
Loads the routers in `api/` via `_load_router()` with status tracking:
- **Required**: auth, recording (simple_recording_db), sessions - fail fast on error
- **Optional**: all others (including satellite_api, websocket_satellite) - log warning, report in /health

`/health` returns `{"status": "healthy"|"degraded", "routers_loaded": N, "routers_failed": [...]}` with real timestamps.

### API Routers (backend/api/)
| Router | Purpose |
|--------|---------|
| simple_recording_db.py | Recording CRUD, audio capture, search, vocabulary integration |
| sessions.py | Session listing and management |
| meeting_management.py | Meeting operations |
| meeting_intelligence_real.py | AI-powered meeting analysis |
| ai_settings.py | AI model configuration |
| agent_management_api.py | Agent CRUD |
| meeting_notes_unified.py | Meeting notes (multi-model) |
| websocket_transcription.py | Live transcription WebSocket |
| websocket_auto_summary.py | Progressive AI summaries |
| unified_agent_api.py | Unified agent config |
| live_transcription.py | Live transcription control |
| analytics_simple.py | Speaker analytics + duration trends |
| ai_insights.py | AI insights (keywords, sentiment, action items via LLM) |
| ai_chat.py | Per-meeting AI chat (real LLM with transcript context) |
| vocabulary.py | Custom vocabulary CRUD |
| batch_export.py | PDF/DOCX/TXT/JSON/SRT export |
| audio_upload.py | Audio file upload |
| simple_settings.py | Settings management |
| satellite_api.py | Satellite device CRUD, heartbeat, audio/transcript upload |
| websocket_satellite.py | Real-time audio streaming from satellite devices |
| websocket_remote_audio.py | Companion app remote audio WebSocket |

### Services (backend/services/)
| Service | Purpose | Status |
|---------|---------|--------|
| unified_llm_service.py | Qwen 3.6 35B-A3B-Vision via the LiteLLM OpenAI-compatible gateway | Working |
| unified_agent_service.py | Progressive / completion-pass summarization | Working |
| working_audio_service.py | USB mic recording via FFmpeg | Working |
| transcription_service.py | Transcription orchestration (server completion pass → Parakeet 1.1B) | Working |
| llm_service.py | LLM service (LiteLLM gateway backend) | Working |
| meeting_notes_service.py | Meeting notes generation | Working |
| auto_summarization_service.py | Auto-summarization | Working |
| live_transcription_service.py | Live transcription service | Working |
| agent_manager_service.py | Agent management | Working |
| websocket_registry.py | WebSocket connection tracking | Working |
| model_providers.py | Model provider config (OpenAI-compatible) | Working |
| settings_manager.py | Settings persistence | Working |

### Database
```
PostgreSQL:
- recording_sessions (id, session_id, name, title, status, created_at, started_at, ended_at, audio_file, transcript, transcript_simple, transcript_diarized, summary, final_summary, duration, ...)
- transcriptions (id, session_id, text, speaker, start_time, end_time, confidence, created_at)
- unified_agents (id, name, system_prompt, model_name, config)
- custom_vocabulary (id, term, expansion, category, priority, is_active, case_sensitive, regex_pattern)
- vocabulary_sets (id, name, description, terms)
```

### Server-side AI services (server completion pass)
- **STT**: Parakeet 1.1B for the server completion pass; in-browser Parakeet for the live transcript
- **Diarization**: pyannote 3.1 via `meet-speaker-svc` (runs on bigboy's RTX 3090)
- **LLM**: Qwen 3.6 35B-A3B-Vision (MoE) via LiteLLM at `unicorn-litellm:4000`; fast model `gemma-4-e4b`
- **Embeddings + reranking**: shared Infinity server — `BAAI/bge-m3` (1024-dim dense) + `bge-reranker-v2-m3`; sparse BM25 local
- **Vector store**: Qdrant, collection `meet_transcripts`, hybrid dense+sparse
- **TTS**: Kokoro (`af_heart`) for vocal summaries

### Config (backend/config/)
- Centralized model configs + `get_active_model()` (Qwen 3.6 35B-A3B-Vision default, `gemma-4-e4b` fast)
- AI backend config points at the LiteLLM OpenAI-compatible gateway

## Known Issues
1. **MeetingIntelligenceDashboard** - Live notes and final report generation endpoints not yet implemented (buttons disabled in UI)

## Testing
```bash
# Run the backend pytest suite
python3 -m pytest tests/ -v
```
Tests in `tests/` cover auth, health/status, recording CRUD lifecycle, export, analytics,
search + AI insights, vocabulary, the session watchdog, and worker↔backend compose env parity.

## Throughput
Measured STT + diarization + LLM throughput (the server completion pass) is captured in
`docs/throughput-benchmark-2026-06-08.md`. A reusable load harness lives at
`services/speaker-svc/scripts/throughput_bench.py`.
