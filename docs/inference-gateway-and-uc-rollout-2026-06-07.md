# Inference gateway + unicorncommander.ai rollout — parked plan (2026-06-07)

> Captured at Aaron's request ("make note of that for unicorncommander.ai and we'll go back to that"). This is the keystone that unblocks the customer-node rollout. NOT built yet.

## The decision (Aaron, 2026-06-07)
Route inference through **`unicorncommander.ai` as a gateway** with automatic fallback:
- **Local-first** → the magicunicorn.dev / bigboy GPU cluster (Qwen 3.6 LLM, Infinity embeddings + reranker, Parakeet STT, Kokoro TTS, pyannote diarization).
- **3rd-party fallback** → OpenAI / Anthropic / etc. when local is unavailable or overloaded.
- Apps point at ONE gateway URL; the gateway decides local-vs-3rd-party. This is the standard "LLM gateway with fallbacks" pattern (LiteLLM, already in the stack, is the natural LLM leg).

## Why this matters now: it fixes the customer-node blocker
Pre-flight (2026-06-07) found the customer node **`unicorncommander.ai` / centerdeep (srv1091360)** at ~v3.26.13, ship-dark, **cannot reach its services from the meet-backend container**:
- Infinity (`infinity:7997` — note: different port than bigboy's `:8082`) → **timed out**
- Parakeet STT (`meet-parakeet-svc:8881`) → **timed out**
- its own Qdrant (`unicorn-qdrant`) → **name resolution failure**

So a v3.28 promotion today would ship a build whose embeddings / RAG / recording can't reach their backends there. The gateway solves this: centerdeep hits **one** reachable gateway endpoint instead of each GPU service directly.

Also note: **Kokoro (vocal summary, midboy2 `192.168.10.14:8880`) and VibeVoice (midboy1) are LAN-local** to the bigboy cluster — a remote VPS like centerdeep can't reach them directly either. The gateway (or a Tailscale-reachable TTS) is the fix.

## Per-service fallback (build order by value)
| Service | Local (bigboy) | 3rd-party fallback | Notes |
|---|---|---|---|
| **LLM** (summaries, titles, RAG, agent chat, spoken script) | Qwen 3.6-35B-A3B | OpenAI / Anthropic / OpenRouter | Easy via LiteLLM fallbacks. Highest value. |
| **Embeddings** | Infinity `bge-m3` (1024-dim) | OpenAI `text-embedding-3-*` | Dim mismatch → keep one canonical model per Qdrant collection, or a translation layer. |
| **Reranking** | Infinity `bge-reranker-v2-m3` | Cohere rerank / skip | Optional; degrade to RRF if absent. |
| **STT** | Parakeet 1.1B | OpenAI Whisper API / Deepgram | More specialized. |
| **TTS** (vocal summary) | Kokoro (AF Heart) | OpenAI TTS / ElevenLabs | LAN-local today; needs gateway or remote Kokoro. |
| **Diarization** | pyannote / sortformer | (no clean 3rd-party) | Hardest to fall back; may stay local-only. |

## Design considerations
1. **The gateway is a critical path** → make it HA, or keep a direct-local bypass for dogfood, so it's not a single point of failure.
2. **Meter the fallback** → 3rd-party calls break the "$0 local/browser-first compute" moat, so fallback usage is exactly what the **credits/metering** work (PO #9) should count. The gateway is the natural metering point.
3. **Latency** → a gateway hop adds latency; co-locate near the GPUs / make routing fast.
4. **Config** → apps set `MEETING_OPS_LLM_*`, `INFINITY_*`, `PARAKEET_*`, `KOKORO_*` to the gateway instead of per-service IPs.

## Rollout checklist (once the gateway is up, to promote centerdeep → v3.28.x)
- Point centerdeep's service env at the gateway (LLM / embeddings / STT / TTS).
- Configure Brigade on centerdeep (`BRIGADE_URL` is currently **unset** there → KG would be empty).
- `git pull` main → v3.28.x; run migrations (043 photo_url + any since v3.26.13).
- Rebuild backend + worker + frontend.
- Recreate the customer Qdrant collection at 1024-dim + reindex its sessions.
- Verify a record → summarize → search round-trip + KG + (vocal summary if TTS reachable).
- Flip the KG nav flag (`VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED=true`) if customer-visible.

## Current state
Dogfood (magicunicorn.dev / bigboy) is on **v3.28.2** — feature-complete + verified (KG, search/RAG indexing fix, Infinity embeddings + reranking, vocal summary, Sessions, Help, watchdog data-loss fix). Customer node promotion is **blocked on this gateway / networking work**, not on app code.
