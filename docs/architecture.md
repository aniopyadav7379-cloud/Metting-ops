# Architecture — Meeting & Lecture Intelligence

## Overview

```
 VOICE / VIDEO / DOCUMENTS
        |
   +----+-----------------------------+
   |                                  |
  OMI (webhook push)          Browser/CLI upload
   |                                  |
   +----------------+-----------------+
                     |
          Transcription + diarization
                     |
           Transcript cleaning
                     |
        Segmentation / chunking
                     |
          Embedding generation
                     |
        +------------+------------+
        |         QDRANT          |   <- persistent semantic memory
        |  (meetings, lectures,   |      one collection, tenant-filtered
        |   documents — one       |      hybrid dense+sparse retrieval
        |   collection)           |
        +------------+------------+
                     |
        Query understanding + retrieval
                     |
        +------------+------------+
        |          LYZR           |   <- reasoning / orchestration
        |  (selectable per-org    |
        |   LLM provider)         |
        +------------+------------+
                     |
   Summary / Q&A / Decisions / Action Items /
   Risks / Important Moments / Lecture Notes
                     |
   Search / Dashboard / Action-item integrations
```

## Layers

- **Frontend** (`frontend/`) — React + TypeScript, HashRouter, Tailwind.
  Pages under `src/pages/`, the sidebar/mobile nav in
  `src/AppRouterSimplified.tsx`.
- **API layer** (`backend/api/`) — one FastAPI router module per concern
  (sessions, lectures, documents, decisions/risks, important moments,
  action items, integrations, auth). Routers are loaded dynamically by
  `main.py`'s `_load_router`, which tolerates missing optional
  dependencies without failing the whole app.
- **AI processing** (`backend/services/`):
  - `services/providers/` — a pluggable provider registry
    (`registry.py`) behind a small `LLMProvider` protocol
    (`protocols.py`): `impl_llm.py` (LiteLLM/OpenAI/Anthropic/etc.),
    `impl_lyzr.py` (the real Lyzr Agent API). Selecting a provider per
    organization changes what *every* call site — summarization, insight
    extraction, Ask AI — actually executes against, with zero branching
    in those call sites.
  - `services/semantic_search_service.py` — the single Qdrant service.
    Meetings, lectures, and documents all go through the same
    `index_session()`/`search_chunks()` calls, tagged with a
    `source_kind` field (`"meeting"` / `"lecture"` / `"document"`,
    defaulting to `"meeting"` for backward compatibility with data
    indexed before that field existed).
  - `services/important_moments.py` — LLM proposes moments; timestamps
    are resolved server-side against real `Transcription` rows, never
    trusted from the model directly.
  - `services/document_knowledge.py` — PDF/DOCX/TXT/MD text extraction
    feeding the same Qdrant service above.
  - `services/integrations/` — `org_config.py` is a generic, encrypted,
    per-organization credential store (used by Jira/Todoist/ClickUp/
    Notion/Confluence/Slack/Teams/Email); `google_calendar.py` is a
    dedicated OAuth2 flow (Calendar requires user-level consent, not an
    org-level API key); `task_providers.py`/`notifiers.py` are the actual
    per-provider API clients.
- **Database** (`backend/database/models.py`, `backend/alembic/`) —
  PostgreSQL in production, SQLite for tests. See `docs/api.md` for the
  data model and `docs/deployment.md` for migration commands.

## Why Omi/Qdrant/Lyzr sit where they do

- **Omi** is a webhook-push platform with no custom-header support, so
  its ingestion credential travels in the URL *path*
  (`/api/v1/integrations/omi/<token>/...`) rather than a header or query
  string — the latter collides with Omi's own appended query params
  (see `docs/omi-integration.md` for the exact platform bug this avoids).
- **Qdrant** is never bypassed: lectures and documents reuse the exact
  same indexing/retrieval code meetings use, distinguished only by
  metadata (`source_kind`), not a parallel pipeline.
- **Lyzr** is used as a reasoning engine over context *this application*
  retrieves and assembles (via Qdrant) — not as its own retrieval/
  knowledge-base layer, and not for internal tool-calling (Lyzr's
  `TOOL_CALLING` feature executes inside Lyzr's own cloud, which is
  incompatible with this app's per-tenant, privately-scoped internal
  operations — see `docs/omi-integration.md`'s Phase 2 addendum for the
  investigated alternative and why it wasn't adopted).

## Data model (new in Phase 1–3)

| Table | Purpose |
|---|---|
| `lecture_details` | Course/instructor/lecture-number + extracted concepts/definitions/examples/questions/study notes, one-to-one with a `recording_sessions` row tagged `meeting_type="lecture"` |
| `important_moments` | Timestamp-grounded moments (decision/action_item/explanation/question/topic_transition/disagreement/conclusion/key_concept) |
| `knowledge_documents` | Uploaded PDF/DOCX/TXT/MD documents, their extracted text, and Qdrant indexing status |
| `google_calendar_integrations` | Per-user OAuth tokens (encrypted), one row per (org, user) |
| `action_items.external_task_refs` | JSON column recording which of the 8 task/notification providers an action item has been pushed to, and the resulting reference |

None of these replace or modify existing meeting/session/action-item
tables beyond additive columns.
