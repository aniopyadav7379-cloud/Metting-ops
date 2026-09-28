# UC Meeting-Ops Code / Architecture / Security Audit

Date: 2026-06-24  
Audited worktree: `fix/upload-status-ws-auth` at `7568e29`  
Method: required architecture/product docs read; auth, WebSocket, upload, worker, retention, deletion, migration, and frontend connection paths inspected; backend test suite invoked with `python3 -m pytest tests/ -q`; Alembic graph checked with `python3 -m alembic heads`.

The separate, already-completed upload summary resilience fix at `48d4be3` (`fix/upload-summary-best-effort`) was reviewed and treated as known/fixed even though it is not an ancestor of this worktree. It is therefore not reported as a finding.

## Executive summary

The codebase has substantially improved controls: JWT expiry is enforced by the shared decoder, `SECRET_KEY` is fail-closed outside explicit development/test environments, the Alembic graph has one head, most meeting WebSockets now use shared JWT handshake authentication, session reads are generally organization-scoped, and the canonical delete path attempts database, object-storage, chat-history, and Qdrant erasure.

The remaining top risks are:

1. Forward-auth identity still fails open when its shared secret is absent, preserving the original forged-header auth/superuser bypass in a default or partially configured deployment.
2. Upload and bulk-import provenance writes the historical recording timestamp into `created_at`; watchdog and retention code interpret that field as processing/ingest age. Historical imports can be prematurely auto-resolved, have local media evicted immediately, or be deleted immediately under retention.
3. The paid server-live WebSocket is broken for native-OIDC clients and ordinary non-superusers: the frontend sends no JWT, the backend only accepts proxy identity, and the resolved user lacks the `_org_ids` required by the tenancy check.
4. TTS job progress has the same silent frontend/backend handshake mismatch: the backend requires `?token=`, while the frontend omits it.
5. The interactive upload queue has no durable claim/idempotency boundary. Duplicate finalize calls, process startup recovery, or multiple application processes can run the same upload concurrently and create duplicate sessions / duplicate GPU work.

The browser-first compute moat is not the cause of these findings. Recommended fixes retain live browser compute and harden only authentication and the existing per-meeting completion pass.

## Findings

### P0

[P0] AuthN / forward-auth trust — `backend/auth/proxy_trust.py:19-25`, `backend/auth/proxy_trust.py:93-96`, `backend/auth/dependencies.py:57-80` — `X-Auth-Request-*` identity is deliberately trusted when `PROXY_AUTH_SHARED_SECRET` is absent. Those plaintext headers can auto-provision a user and derive global-admin status from attacker-supplied groups. — Any caller that can reach the backend directly in an unset/partially configured deployment can authenticate as an arbitrary email and request superuser provisioning. This is the exact trust-boundary vulnerability the module documents, but the default remains vulnerable. — Fail closed by default: refuse to honor forward-auth headers unless a strong shared secret is configured and matches. If staged rollout is still needed, make fail-open an explicit development-only flag and add a production boot guard comparable to `SECRET_KEY`. — effort(S)

[P0] Data integrity / retention — `backend/api/uploads.py:1098-1129`, `backend/api/uploads.py:1131-1141`, `backend/services/bulk_import_queue.py:469-500`, `backend/services/session_watchdog.py:542-547`, `backend/services/data_retention.py:70-74`, `backend/services/media_retention.py:92-97` — Upload and bulk-import creation set `created_at` and `started_at` to the inferred historical recording time, despite having separate `meeting_date` / `meeting_time` and a provenance ledger containing `uploaded_at`. Watchdog and retention code treat `created_at` as operational age. — Importing an old recording can immediately qualify as “stuck processing”; after completion it can immediately qualify for local-media eviction and, when retention is enabled, canonical deletion. This can silently truncate a running completion pass or delete newly imported data. — Keep `created_at` as server ingest time and use `meeting_date` / `meeting_time` plus `processing_metadata.meeting_provenance.when.recorded_at` for the historical meeting timestamp. Use `updated_at`/`job_started_at` for processing watchdog age and `ended_at` or an explicit retention anchor for retention. Backfill affected rows before enabling the corrected policy. — effort(M)

[P0] WebSocket auth / server-live correctness — `frontend/src/components/ServerLiveTranscript.tsx:239-243`, `frontend/src/pages/StreamingTest.tsx:488-498`, `backend/api/streaming.py:340-405`, `backend/api/streaming.py:951-964`, `backend/auth/ws_auth.py:119-130`, `backend/api/streaming.py:1020-1024` — The server-live client omits `?token=`; the backend resolves only oauth2-proxy headers, not the native-OIDC/app JWT. Even on the proxy path, `_resolve_ws_user` detaches the user without materializing `_org_ids`, then `ws_user_can_access_session` denies every existing-session connection for a normal user. — Native-OIDC production clients cannot connect at all, and proxy-authenticated non-superusers are rejected by the tenancy check. This is silent breakage of the paid server-live path. — Replace the bespoke resolver with `auth.ws_auth.authenticate_ws`/`enforce_ws_auth`, retaining trusted forward-auth only as an explicitly verified alternative if required. Append the JWT and active-org selector in `ServerLiveTranscript` and its test page. Add integration tests for native-OIDC JWT, ordinary in-org user, cross-org user, and superuser. — effort(M)

[P0] WebSocket auth / TTS correctness — `backend/api/tts.py:822-855`, `frontend/src/pages/SessionDetails.tsx:507-535` — `/ws/tts/{job_id}` requires shared JWT handshake auth, but `trackTtsJob` constructs the URL without `?token=` (and without the active-org selector). — With `WS_REQUIRE_AUTH=true`, every browser TTS progress socket closes with 1008, so vocal-summary/podcast jobs surface as socket failures even if the worker completes successfully. — Build this URL through the same `appendWsToken(getOrgQueryUrl(...))` helper used by recording sockets; add a frontend test asserting token and org query parameters. — effort(S)

### P1

[P1] AuthN / native OIDC — `backend/auth/oidc_sso.py:118-138`, `backend/auth/oidc_sso.py:140-154` — The callback parses `id_token` with `jwt.get_unverified_claims` and then trusts email and groups to provision users and superusers. It does not validate signature, issuer, audience, authorized party, expiry, or nonce. — A compromised/misrouted token response, Keycloak/client configuration mistake, or future callback refactor can turn unverified claims into account takeover or global-admin escalation. State protects CSRF but does not validate token authenticity or bind the ID token to this browser flow. — Use OIDC discovery/JWKS verification with exact issuer and client audience checks, validate `exp`/`iat`/`azp`, issue and verify a nonce, and reject malformed group claim types. Prefer a maintained OIDC client implementation over handwritten token validation. — effort(M)

[P1] Tier authorization / server compute — `backend/api/streaming.py:966-989`, `backend/api/websocket_remote_audio.py:252-270`, `backend/auth/tier.py:412-419`, `backend/auth/tier.py:443-452` — Server-live gates only the user tier, and remote-audio calls `gate_feature_for_caller` without an active/bare organization. Both omit the authoritative active-workspace plan check. — A paid user can consume live STT / completion compute while operating on a Free workspace, bypassing per-workspace billing and quota intent. — Resolve the session first, load its organization, and require both user capability and `org_covers_feature` before accepting audio. Derive rate-limit/metering buckets from the session organization, not a detached user fallback. — effort(M)

[P1] WebSocket compute authorization — `backend/auth/ws_auth.py:85-130`, `backend/api/websocket_transcription.py:140-149` — Session authorization deliberately allows an unresolvable session id, and the legacy transcription WebSocket accepts it without a paid-tier gate before allowing audio to drive server STT. — Any authenticated account, including Free, can create arbitrary socket identifiers and consume server transcription compute outside an owned meeting and outside the browser-first/free-tier boundary. — Require the session to exist for compute-bearing sockets, scope it to the caller’s organization, and enforce the relevant user+workspace feature before accept. Keep “not found” and “forbidden” indistinguishable with the same close code. — effort(S)

[P1] Upload security / resource exhaustion — `backend/api/uploads.py:474-503`, `backend/api/uploads.py:509-535` — A chunk is read fully into memory with `await data.read()` and written without checking its size against `DEFAULT_CHUNK_SIZE`, remaining declared bytes, or the configured/tier cap. Enforcement occurs only after all chunks are assembled. — An authenticated user can submit a single multi-gigabyte multipart chunk despite declaring a small upload, causing memory pressure, disk exhaustion, expensive directory scans, and a final size-mismatch error only after resources are consumed. — Stream each chunk to a temporary file in bounded blocks, reject once the expected per-index size or remaining total is exceeded, atomically rename on success, and verify `bytes_received <= total_size <= quota` transactionally. — effort(M)

[P1] Upload idempotency / concurrency — `backend/api/uploads.py:241-290`, `backend/api/uploads.py:509-547`, `backend/api/uploads.py:304-324`, `backend/api/uploads.py:831-880`, `backend/api/uploads.py:1217-1221` — Finalize has no stage compare-and-set or idempotency key; every call re-enqueues the upload. The in-process queue has no distributed claim, and every application process runs startup recovery. A recovered `transcribe` job always creates a new session even when `job.session_id` already points to one from a prior partial run. — Duplicate requests, multi-worker startup, or restart during a later stage can create duplicate sessions, re-run Parakeet/pyannote/Qwen, overwrite job state out of order, and leave the original session stuck in processing. — Move interactive uploads to Arq (or use a DB `SELECT ... FOR UPDATE SKIP LOCKED`/atomic claim), make finalize an atomic `uploading -> queued` transition, assign a stable job id, and make `_resolve_session_for_job` resume the existing org-scoped session. Persist per-stage completion and skip already committed stages safely. — effort(L)

[P1] Completion-pass degradation / retryability — `backend/api/uploads.py:891-922`, `backend/api/uploads.py:973-1019`, `backend/api/uploads.py:1021-1028` — Diarization/identification and semantic indexing exceptions are swallowed, their stages are effectively treated as complete, and the upload is marked `done` without `needs_diarization` / `needs_index` state or a retry job. — Meetings can permanently finish with generic/no speakers or remain absent from cross-meeting search/RAG while the UI reports a successful completion. A transient service outage becomes durable incomplete data. — Record explicit degraded-stage metadata and error text; enqueue idempotent stage-specific retries that reuse persisted STT output and do not re-transcribe. Expose “completed with warnings” in the status payload. — effort(M)

[P1] Long-meeting summary correctness — `backend/api/uploads.py:2167-2192`, `backend/api/uploads.py:2206-2209`, `backend/api/uploads.py:2428-2447` — Map calls return `None` on individual failures; the reducer proceeds as long as one digest succeeded, and the caller then sets `truncated=False` and records full map-reduce coverage. — A partial outage can silently omit one or more time ranges from the final summary while metadata claims the entire meeting was covered. — Require all chunks, or explicitly retry failed chunks and record missing ranges. If reducing partial output is allowed, stamp `summary_truncated=true`, failed chunk indexes/ranges, and surface a warning. — effort(S)

[P1] Bulk import durability / worker isolation — `backend/api/imports.py:347-355`, `backend/services/bulk_import_queue.py:275-306`, `backend/workers/bulk_import_worker.py:212-220`, `backend/workers/bulk_import_worker.py:224-256`, `backend/workers/bulk_import_worker.py:261-270` — The upload API always calls the process-local `bulk_import_queue.submit`; it never uses `enqueue_file`, even when Arq is enabled. The task changes the row to `processing`, while Arq recovery scans only `queued`/`uploading`. — Bulk processing still consumes API-process resources, is lost on an API restart, and a crash after the status flip leaves the file permanently stranded in `processing`. The dedicated batch worker lane is bypassed. — Route API submission through `enqueue_file` when Arq is enabled, use a stable Arq `_job_id`, recover stale `processing` rows with a lease/heartbeat, and reserve the in-process queue strictly for explicit `ARQ_ENABLED=false` deployments. — effort(M)

[P1] Data erasure — `backend/api/simple_recording_db.py:2246-2254`, `backend/api/simple_recording_db.py:2274-2288`, `backend/auth/routes.py:679-718` — Session/account deletion commits the relational deletion even when Garage, Qdrant, Brigade, or Keycloak erasure fails; failures are only logged and no tombstone/outbox retains enough state for retry. — Transcript-derived PII or biometric/media data can survive a successful deletion response. Because the authoritative row and storage pointers are gone, automated reconciliation becomes harder and the user receives a false completion signal. — Persist an erasure job/tombstone containing org and external object identifiers before deleting the row, process it idempotently with retries, and expose pending/failed erasure status. For strict deletion endpoints, return accepted/pending until all required stores confirm deletion. — effort(L)

[P1] Multi-org deletion isolation — `backend/auth/routes.py:635-667` — Account deletion removes `ChatHistory` by `session_key` only, without pairing each key with its organization. Legacy sessions can use the integer primary key string as the canonical key, which is not globally unique across tenants. — Deleting one account can delete another organization’s per-meeting chat history when canonical keys collide; this is a cross-tenant destructive write. — Delete by `(organization_id, session_key)` pairs, grouped per organization, or add a real session foreign key/composite unique key to chat history and cascade from the meeting. — effort(S)

### P2

[P2] Personal access tokens — `backend/auth/models.py:175-190`, `backend/auth/pat.py:62-83`, `backend/api/personal_access_tokens.py:20-38` — PATs have no expiration, scope, or organization restriction; resolution checks only hash, revocation, and user activity. — A leaked PAT remains a full-account credential indefinitely and can follow the user into every organization they later join. — Add optional/default expiry, explicit scopes, and allowed organization ids; enforce them in the dependency layer and show expiry/last-used information in the UI. — effort(M)

[P2] Upload resilience / subprocess lifecycle — `backend/api/uploads.py:1062-1082` — ffmpeg extraction has no wall-clock timeout, input-duration/output-size ceiling, or cancellation cleanup that terminates the child process. — Malformed media can hold an upload worker indefinitely; cancelling the Python task can leave ffmpeg consuming CPU/disk after the job is marked cancelled. — Wrap extraction in a timeout based on declared/inspected duration, terminate then kill the process on timeout/cancellation, remove partial output, and record a retryable extraction error. — effort(S)

[P2] Legacy export correctness — `backend/api/meeting_management.py:149-165`, `backend/api/meeting_management.py:187-219`, `backend/database/models.py:340-360` — TXT/JSON/SRT export falls back to `t.speaker_id`, but the live `Transcription` model explicitly has no `speaker_id` column. — Any segment with a null `speaker` raises `AttributeError`, turning an otherwise valid export into a 500. — Use a deterministic generic label from the loop index or the shared speaker-label renderer; add a null-speaker export test. — effort(S)

## Quick wins

1. Make proxy forward-auth fail closed unless an explicit development-only override is set.
2. Append JWT + org query parameters to TTS and server-live WebSocket URLs.
3. Materialize `_org_ids` or adopt the shared WS authenticator in `streaming.py`.
4. Pass the session organization into server-live and remote-audio tier gates.
5. Enforce bounded streaming writes for upload chunks.
6. Make upload finalize an atomic one-way state transition.
7. Require complete map chunks or mark summaries partial.
8. Scope account-delete chat-history removal by organization.
9. Add timeout/kill cleanup around ffmpeg.
10. Remove the nonexistent `Transcription.speaker_id` export fallback.

## Strategic fixes

1. Move interactive upload processing from per-process asyncio queues to the existing Arq infrastructure, with stable job ids, leases, and per-stage resumability.
2. Separate operational timestamps (`created_at`, `processing_started_at`, `completed_at`) from semantic meeting time (`meeting_date`, `meeting_time`, provenance `recorded_at`) and migrate affected records.
3. Build a durable erasure outbox/tombstone workflow spanning PostgreSQL, Garage, Qdrant, Brigade, and Keycloak.
4. Consolidate WebSocket authentication and authorization into one implementation that supports app JWT/native OIDC, trusted proxy identity, session existence, organization membership, and workspace tier checks.
5. Add database-enforced tenant isolation for high-value tables (RLS or mandatory repository/query helpers), beginning with sessions, chat history, speakers/voice samples, upload jobs, and action items.

## Verification notes

- Alembic: `python3 -m alembic heads` returned one head: `049_room_retention_opt_in`.
- Migration parent scan found no missing parent revisions.
- Upload path inspection found no archive extraction and no user-controlled URL fetch, so no zip-bomb or SSRF finding is reported.
- Filename/path construction uses basename sanitization and server-generated upload ids; no path traversal was verified in the inspected chunked-upload or bulk-import endpoints.
- Backend collection found 953 tests. The requested `python3 -m pytest tests/ -q` command resolved to Python 3.14.4 on this host (the product target is Python 3.13). It was interrupted after roughly 30 minutes at 22% because the run rate projected to multiple hours; tests were still progressing rather than deadlocked. At interruption, 11 failures had been recorded:
  - `tests/test_account_self_delete.py`: four failures. The common exception was `sqlalchemy.exc.InvalidRequestError` at `backend/auth/routes.py:679`: the `User` instance supplied by auth was attached to a different SQLAlchemy session than the endpoint's `db`. This may be test dependency-session behavior, but the endpoint is not robust to it; verify under the Python 3.13 project environment.
  - `tests/test_agent_actions.py`: three failures.
  - `tests/test_agent_drift.py`: one failure.
  - `tests/test_agent_write_tools_v16.py`: three failures.
  - The agent-action/write failures shared `sqlalchemy.exc.NoReferencedTableError`: `recording_sessions.room_source_id` could not resolve `room_audio_sources.id` during flush. This demonstrates import-order-dependent SQLAlchemy metadata registration in the tested execution path.
- Because the suite was interrupted, the observed 11 failures are a lower bound, not a complete suite result. Re-run in the project's Python 3.13 environment before release.

## Test coverage gaps tied to findings

- WebSocket tests assert unauthenticated rejection for TTS/uploads but do not render the frontend URL builders, so the missing TTS token is not caught (`backend/tests/test_ws_auth.py:113-118`).
- Server-live has no end-to-end test for native-OIDC JWT auth plus ordinary organization membership.
- Upload recovery tests only assert re-enqueue, not that a later-stage recovery reuses the existing session (`backend/tests/test_upload_recovery.py:9-46`).
- Proxy-trust tests explicitly lock in fail-open behavior instead of asserting production fail-closed boot/configuration (`backend/tests/test_proxy_trust.py:41-57`).
- Retention tests do not create a freshly ingested historical upload whose `created_at` is old but `uploaded_at` is current.
- No test concurrently calls upload finalize or starts recovery from two process contexts.
- No test verifies all map-reduce chunks contributed to a summary before `summary_truncated` is cleared.
- Bulk-import tests do not verify that the HTTP upload path enqueues onto Arq when Arq is enabled.
