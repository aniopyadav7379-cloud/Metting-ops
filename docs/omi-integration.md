# Omi + Lyzr integration (vertical slice)

This document covers the two integrations added on top of the existing
Meeting-Ops architecture: **Omi** (real-time voice capture ingestion) and
**Lyzr** (agent orchestration for reasoning/extraction). Both are optional —
the app works exactly as before if neither is configured.

## Why these boundaries, specifically

Meeting-Ops already had the two hardest pieces genuinely built: a pluggable
per-org `LLMProvider` (`services/providers/`) and a real hybrid Qdrant
retrieval service (`services/semantic_search_service.py`). Neither Omi nor
Lyzr needed a new pipeline; they needed to be plugged into the existing one
at the correct seam, without rewriting either.

```
 Omi (device/app)                                   Existing pipeline
 ─────────────────                                  ──────────────────
 Real-Time Transcript webhook  ──┐
                                  ├─► api/omi_webhooks.py ──► RecordingSession
 Memory Creation webhook       ──┘        (live segments,        (mode=live/upload,
                                            or finalize)           external_source=omi)
                                                │
                                                ▼
                                  _summarize_session() / _generate_ai_insights()
                                  (UNCHANGED — same functions api/uploads.py uses)
                                                │
                                                ▼
                                  registry.get_llm(org) ──► LyzrProvider (if org
                                                              selected provider_name="lyzr")
                                                │
                                                ▼
                                  semantic_search.index_session() (UNCHANGED,
                                  real Qdrant hybrid dense+sparse+RRF, tenant-filtered)
```

For **Ask AI** (`POST /api/ai-chat/rag/query`), nothing new was built at
all: it already does query-rewrite → `semantic_search.search_chunks()` →
context assembly → `registry.get_llm()` → grounded answer. Selecting
`provider_name="lyzr"` in Provider Settings makes that whole existing
pipeline genuinely execute through Lyzr — this is "Lyzr orchestration" in
the sense the platform actually supports (Lyzr reasons over context you
retrieve and assemble; it doesn't run its own retrieval against your Qdrant
cluster).

## Omi

Omi (docs.omi.me) is a webhook-**push** platform: you configure a URL inside
an "Integration App" in the Omi mobile app, and Omi calls it. There is no
persistent connection to hold open, and Omi does not support custom request
headers on its webhooks — which rules out this codebase's usual
`Authorization: Bearer <PAT>` pattern (see `api/stable_ingest.py`).

Two further constraints shaped the design:

1. **No custom headers** → the integration credential has to travel in the
   URL. A query parameter (`?token=...`) would collide with the query
   parameters Omi itself appends (`?session_id=...&uid=...` /
   `?uid=...`) — and there is an open Omi platform bug
   (BasedHardware/omi#11365) where Omi appends its own params with `?`
   instead of `&` when the configured URL already has a query string,
   producing an unparseable URL. Putting the token in the **path** instead
   (`/api/v1/integrations/omi/<token>/realtime-transcript`) means Omi's
   appended query string is the request's only `?`, sidestepping the bug
   entirely.
2. **Auth model reuse** → the token is an ordinary Personal Access Token
   with a new scope, `omi.transcript.ingest`, bound to one organization —
   same table, same hashing, same revocation path as every other PAT.

### Setup

1. `POST /api/auth/pats` with `{"scope": "omi.transcript.ingest", "organization_slug": "<your-org>"}` (or via Settings → Personal Access Tokens in the UI once exposed there) → copy the returned `plaintext`.
2. In the Omi app, create an Integration App and set:
   - Real-Time Transcript webhook: `https://<host>/api/v1/integrations/omi/<token>/realtime-transcript`
   - Memory Creation webhook: `https://<host>/api/v1/integrations/omi/<token>/memory-created`
3. Start talking. Live segments create/append to a `RecordingSession` with `mode="live"`, `source_type="omi"`; the live-transcript UI updates via the existing websocket broadcaster (`api/websocket_transcription.py`) on a best-effort basis. When Omi finalizes the conversation, the Memory Creation webhook finalizes the session (transcript persisted, then the real summarize → insights → Qdrant-index pipeline runs) and marks it `status="completed"`.

Both endpoints are idempotent: redelivered real-time segments are deduped by
a content hash; a redelivered Memory Creation payload is deduped by Omi's
own conversation id (`external_source="omi"`, `external_id=<omi id>`,
unique per org).

### Known limitation

Omi's own structured summary/action-items (sent in the Memory Creation
payload) are stored for reference (`extra_data.omi_structured`) but are
**not** treated as authoritative — this platform's own extraction (via
`_generate_ai_insights`, optionally Lyzr-backed) runs instead, so an
Omi-sourced meeting has the same traceability guarantees (source
chunk → transcript segment → timestamp) as every other ingestion path.

## Lyzr

Lyzr's real API surface (`https://agent-prod.studio.lyzr.ai`) is an agent +
chat model: `POST /v3/agents/` creates a named agent (system prompt, model,
temperature, top_p fixed at creation time), `POST /v3/inference/chat/`
converses with it. There is no per-message `top_p`/`top_k` override and no
OpenAI-style function-calling — which is why Lyzr is wired in as an
`LLMProvider` (satisfies `chat`/`chat_sync`/`chat_stream`/`health`) rather
than dropped into `services/agents/meeting_rag.py`'s tool-calling loop,
which assumes an OpenAI-compatible `/chat/completions` endpoint.

- `services/providers/impl_lyzr.py` — the real client. One agent is
  provisioned per organization on first use and cached in
  `OrgProviderSettings.overrides["lyzr_agent_id"]` (idempotent — never
  recreated on every call).
- `services/providers/registry.py` — `provider_name="lyzr"` dispatches here.
- `api/provider_settings.py` — `"lyzr"` is now a selectable `provider_name`
  for `service_kind="llm"`.

### What this means concretely, once an org selects Lyzr

- `api/uploads.py::_summarize_session` (meeting/lecture summaries) → Lyzr.
- `api/ai_insights.py::_generate_ai_insights` (action items, key decisions,
  follow-ups/risks) → Lyzr.
- `api/ai_chat.py::rag_query` ("Ask AI") — both the query-rewrite call and
  the final grounded-answer call → Lyzr. Qdrant retrieval underneath is
  **unchanged**.

### Required configuration

`LYZR_PROVIDER_ID` and `LYZR_LLM_CREDENTIAL_ID` (environment) are
account-level IDs from the customer's own Lyzr Studio account (Settings →
LLM Credentials) — these cannot be inferred, so `LyzrProvider` raises
`LyzrNotConfigured` (and `health()` reports `available: false`) rather than
silently falling back or fabricating success when they're unset. The API
key itself is entered per-organization in Provider Settings, encrypted at
rest like every other provider's key.

### Known limitation — verification in this environment

This sandbox's network egress does not include `agent-prod.studio.lyzr.ai`
or Omi's API — confirmed directly (a live test run against
`agent-prod.studio.lyzr.ai` was rejected by this sandbox's own egress proxy:
`403 Host not in allowlist`). Every request shape here is built exactly to
Lyzr's and Omi's own published API contracts and is covered by tests that
mock only the HTTP transport boundary (`tests/test_lyzr_provider.py`,
`tests/test_lyzr_summarization_e2e.py`, `tests/test_ask_ai_e2e.py`,
`tests/test_omi_webhooks.py`), but genuine live connectivity to Lyzr's and
Omi's cloud services has not been (and could not be) exercised from this
sandbox. Verify against the real services in a deployment that has network
access to them before relying on this in production.

## Phase 2 addendum — Lyzr tool-calling investigated, not adopted

Lyzr agents do support a real, documented `TOOL_CALLING` feature module
(register an OpenAPI spec against the agent in Lyzr Studio; the agent calls
it mid-reasoning) and a `KNOWLEDGE_BASE` module (Lyzr's own hosted
vector-store RAG). Both were investigated against this codebase's actual
needs and deliberately **not adopted**:

- **`TOOL_CALLING`**: Lyzr executes registered tools *inside Lyzr's own
  cloud infrastructure* during its reasoning loop — the caller sends one
  message and gets back a final answer; there is no intermediate "here is
  the tool call, you execute it locally and send back the result" round
  trip the way OpenAI-style function calling (and this codebase's own
  `services/agents/meeting_rag.py` tool loop) works. `meeting_rag.py`'s
  tools run inside our own authenticated request context, scoped to the
  caller's already-resolved org/permissions, against private per-tenant
  data. Routing that through Lyzr's `TOOL_CALLING` would mean exposing
  those internal operations as a public OpenAPI surface Lyzr's cloud can
  call directly — a materially larger attack surface and a multi-tenant
  authorization redesign, not a drop-in replacement. `meeting_rag.py`'s
  existing tool-calling mechanism is kept, unmodified, for exactly this
  reason: it is the technically stronger fit for privately-scoped internal
  operations.
- **`KNOWLEDGE_BASE`**: would mean duplicating tenant transcript data into
  Lyzr's own hosted vector store — directly conflicting with "never replace
  ZIP 3's Qdrant implementation" and with the tenant-isolation guarantees
  already enforced in `semantic_search_service.py`'s Qdrant filters. Not
  adopted; Qdrant remains the sole vector memory, exactly as in Phase 1.

Net effect: Lyzr is used strictly as a reasoning engine over context this
application retrieves and assembles itself (Qdrant → context → Lyzr
`chat`), which is also the shape Lyzr's own `/v3/inference/chat/` endpoint
is built around. No second orchestration path was created; this is the same
`LyzrProvider` from Phase 1.

