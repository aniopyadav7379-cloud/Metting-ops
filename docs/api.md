# API Overview — Meeting & Lecture Intelligence additions

This covers the endpoints added across Phases 1–3. For the pre-existing
Meeting-Ops surface (sessions, transcription, ai-insights, ai-chat/RAG,
provider settings, PATs), see the inline docstrings in `backend/api/` —
those routers were kept as-is.

All endpoints below require authentication (`get_current_user`) and are
scoped to the caller's active organization (`get_current_organization`)
unless noted.

## Omi integration — `api/omi_webhooks.py`

- `POST /api/v1/integrations/omi/{token}/realtime-transcript` — live
  transcript segments as Omi produces them. `{token}` is a PAT with scope
  `omi.transcript.ingest` (create via `POST /api/auth/pats`). Idempotent
  per-segment (deduped by content hash).
- `POST /api/v1/integrations/omi/{token}/memory-created` — finalizes a
  completed Omi conversation into a `RecordingSession` and runs the
  existing summarize → insights → Qdrant-index pipeline. Idempotent by
  Omi's own conversation id.

## Lectures — `api/lectures.py`

- `GET /api/lectures` — list lectures (`meeting_type="lecture"` sessions),
  optional `?course=`.
- `PUT /api/lectures/{session_id}/mark` — tag a session as a lecture and
  set course/subject/instructor/lecture_number. Re-indexes the session
  into Qdrant with `source_kind="lecture"`.
- `GET /api/lectures/{session_id}` — full lecture detail.
- `POST /api/lectures/{session_id}/extract` — run (or re-run) concept/
  definition/example/question/study-notes extraction over the transcript.

## Documents — `api/documents.py`

- `POST /api/documents/upload` (multipart) — PDF/DOCX/TXT/MD only, 20MB
  max. Deduplicated by content hash per organization. Indexes into Qdrant
  with `source_kind="document"` on success.
- `GET /api/documents` / `GET /api/documents/{doc_id}` — list/get.
- `POST /api/documents/{doc_id}/reindex` — re-run indexing from stored text.
- `DELETE /api/documents/{doc_id}` — removes the DB row and its Qdrant points.

## Decisions & Risks — `api/decisions_risks.py`

- `GET /api/decisions` / `GET /api/risks` — flattened, source-attributed
  lists drawn from each session's `ai_insights.key_decisions` /
  `.follow_ups`. Optional `?session_id=` filter.

## Important Moments — `api/important_moments.py`

- `POST /api/moments/{session_id}/extract` — generates moments; replaces
  any previously-extracted moments for that session.
- `GET /api/moments/{session_id}` — moments for one session.
- `GET /api/moments` — org-wide timeline, optional `?moment_type=`.

## Google Calendar — `api/google_calendar.py`

- `GET /api/integrations/google-calendar/status`
- `GET /api/integrations/google-calendar/authorize` — returns the Google
  consent URL; 503 if `GOOGLE_OAUTH_CLIENT_ID`/`SECRET`/`REDIRECT_URI`
  aren't set.
- `GET /api/integrations/google-calendar/callback` — OAuth redirect target.
- `POST /api/integrations/google-calendar/disconnect`
- `POST /api/integrations/google-calendar/action-items/{id}/create-event`
  — the explicit user-confirmation step; nothing creates a calendar event
  automatically.

## Task/notification integrations — `api/action_item_integrations.py`

- `POST /api/action-items/{id}/push/{provider}` for
  `jira|todoist|clickup|notion|confluence` — body carries the
  provider-specific target (`project_key`, `list_id`, `database_id`, or
  `space_key`).
- `POST /api/action-items/{id}/notify/{provider}` for
  `slack|teams|email` — `email` additionally requires `to_address`.
- Both record a ref (id/url/timestamp) onto
  `action_items.external_task_refs[provider]` on success, and return
  `{"ok": false, "error": "..."}` (HTTP 200) rather than raising when the
  provider isn't configured or the API call fails — the request itself
  succeeded; the sync attempt is what's reported.

## Provider configuration — `api/integrations_org.py` (existing, extended)

- `GET/PUT/DELETE /api/integrations/me/{integration}` and
  `POST /api/integrations/me/{integration}/test` — generic, encrypted,
  per-org config CRUD, now also covering `jira`, `todoist`, `clickup`,
  `notion`, `confluence`, `slack`, `teams`, `email` alongside the
  pre-existing `brigade`/`project_ops`/`contact_ops`/`accounting_ops`/`stable`.
