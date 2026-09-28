# Security — Meeting & Lecture Intelligence additions

## Tenant isolation

Every Qdrant point carries `organization_id` in its payload, and every
retrieval path (`semantic_search_service.search_chunks`) applies a
`Filter` on it — meetings, lectures, and documents are all subject to the
same filter, verified explicitly with two-organization tests for each
source type (`tests/test_qdrant_real_roundtrip.py`,
`tests/test_documents.py`, `tests/test_cross_source_ask_ai.py`). No
retrieval path accepts a client-supplied organization id; it always comes
from the authenticated session's active organization.

## Authentication & authorization

- Omi ingestion: a dedicated PAT scope (`omi.transcript.ingest`), bound to
  one organization, validated (hash + expiry + revocation) the same way
  every other PAT in this codebase is. Tested: invalid, revoked, expired,
  and wrong-scope tokens are all rejected (`tests/test_phase2_security.py`,
  `tests/test_omi_webhooks.py`).
- Every new API router (`lectures`, `documents`, `decisions_risks`,
  `important_moments`, `google_calendar`, `action_item_integrations`) uses
  the existing `get_current_user` + `get_current_organization`
  dependencies — no new auth mechanism was introduced.
- Google Calendar OAuth: CSRF-protected via a single-use, TTL'd state
  token minted only for an already-authenticated user; the callback route
  itself has no user dependency (Google's redirect can't carry a bearer
  token) but the state token is what proves which user/org it belongs to.

## Secret handling

- Provider API keys, Google Calendar OAuth tokens, and Jira/Todoist/
  ClickUp/Notion/Confluence/Email credentials are all encrypted at rest
  via `services/providers/crypto.py` (Fernet) — the same helper used for
  every pre-existing provider credential, not a new encryption scheme.
- PAT plaintext is never stored — only its hash — and never appears in any
  list/get response (`tests/test_phase2_security.py::test_pat_plaintext_never_appears_in_list_response`).
- Slack/Teams webhook URLs are treated as the secret itself (no separate
  API key) and stored encrypted the same way.
- SMTP passwords are stored via the same `api_key_encrypted` field/helper
  every other provider secret uses.
- Nothing in `.env.example` contains a real value.

## AI output validation / grounding

- Important Moments: an LLM-claimed quote is matched against real
  `Transcription` rows server-side; an unmatched quote gets
  `timestamp: null`, never a fabricated number
  (`tests/test_important_moments.py`).
- Action items / decisions / risks: extracted via the existing
  `api/ai_insights.py` JSON-prompt-and-parse pipeline (unchanged);
  `owner`/`deadline` are `null` when the transcript doesn't support them,
  not guessed.
- Ask AI: an empty or off-topic retrieval result surfaces an explicit "no
  evidence" answer rather than a hallucinated one, tested for both the
  fully-empty-knowledge-base case and the has-content-but-irrelevant case
  (`tests/test_cross_source_ask_ai.py`).
- Lyzr responses are never trusted as well-formed by default: malformed
  JSON, missing expected keys, and non-2xx responses are all handled
  without crashing the request (`tests/test_phase2_security.py`,
  `services/providers/impl_lyzr.py`'s permanent-vs-transient error
  classification).

## Malformed / oversized input handling

- Omi webhook payloads: Pydantic field length caps (segment text ≤8000
  chars, ≤500 segments per real-time call, ≤5000 per memory-created call)
  reject oversized payloads with 422, not a memory-exhaustion risk.
- Document uploads: 20MB cap, unsupported extensions rejected with 422,
  empty files rejected.
- Provider API failures (Jira/Todoist/ClickUp/Notion/Confluence/Slack/
  Teams/Email/Google Calendar/Lyzr) are all caught and reported as
  `{"ok": false, "error": ...}` rather than raising an unhandled exception.

## Known gaps (not fixed this phase)

- The Google Calendar OAuth state store is in-process memory — correct
  for a single-worker deployment, but would need to move to Redis/DB for
  a multi-worker one (noted in `api/google_calendar.py`).
- No rate limiting was added specifically for the new endpoints; they rely
  on whatever global rate limiting the deployment already applies.
