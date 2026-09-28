# Bulk Audio Import: the `/import` Page

Status: Draft for approval. Doc-only work; no code in this commit.
Implementation tickets at the end of this doc.
Owner: Meeting-Ops team.
Authoring date: 2026-05-22.

## 1. TL;DR

UC-Meeting-Ops today ingests audio one file at a time. The single-file
upload at `/sessions` works well for a meeting you just finished, but it
falls over the moment you point it at a folder. Aaron has a 526-file,
~8 GB archive of voice memos and Mac Notes audio at
`/Volumes/media/audio-from-notes-voicememos-2026-05-20` (date range
2024-09 to 2026-05). Every one of those files matches the
`{notes|downloads}__YYYY-MM-DD_HHMMSS__title.{m4a|mp3}` filename pattern
that today's commit `e447cf9` taught the platform to parse. The corpus
exists; the platform can read its metadata; nothing currently lets the
user drop the folder into a browser tab.

This doc designs a new `/import` page that closes that gap. The page
takes a folder (or a multi-file picker), runs the filename parser
client-side to show a preview table, lets the user confirm or override
per-row metadata, then streams the audio up serially with a strict
concurrency limit on the reprocess pipeline so 526 simultaneous Parakeet
calls don't OOM the box. The same machinery is the foundation for any
later bulk-ingest workflow (legal discovery dumps, podcast back-catalog
imports, conference-room archive backfills). For Aaron specifically it
turns ~5-9 hours of manual single-file uploads into one drag-and-drop
plus an overnight batch.

## 2. Why we're building this now

Three things converged:

1. **Today's commit `e447cf9`** added `meeting_date` + `meeting_time`
   columns on `recording_sessions` plus the shared filename parser
   (`backend/utils/filename_parser.py` + `frontend/src/utils/filenameParser.ts`).
   Pattern 1 in that parser hits all 528 files in the corpus with
   confidence 1.0. The metadata pipeline is already in place; only the
   bulk UX is missing.

2. **The corpus**: 526 `.m4a` + 2 `.mp3` files, ~8 GB, two years of
   meetings, calls, brainstorms, and dictation. Many are
   `Call with {Person}` (titled by Aaron at record time on his Mac). If
   we don't import these, they sit on an external drive forever and the
   platform's knowledge graph (Brigade integration, AI Chat across
   meetings, RAG over the catalog) has a two-year hole in it. If we
   import them naively (526 simultaneous reprocesses) we OOM the GPU
   pool and break live traffic for paying users.

3. **The reprocess pipeline is expensive per call**: Parakeet 1.1B fp16
   on the RTX 6000, pyannote 3.1 + wespeaker on midboy2, Qwen 3.6
   35B-A3B-Vision on midboy1 P40 via LiteLLM. Realistic per-meeting time
   is 30-90 seconds of GPU. At 526 files that's 5-9 hours serial, 2.5-4.5
   hours at concurrency=2. Worth doing right.

## 3. UX flow

### Entry point

A new top-level nav item in the sidebar at `/import`, labeled "Bulk
Import", placed under "Record" and above "Sessions". Available to all
authenticated users on day one. We initially considered admin-only
gating because of the cost profile, but per-org tier quotas
(`services/quotas.py` already exists) are the right control surface here.
A Pro user with a 5000-file archive should be able to use this page; the
tier ceiling decides what's reasonable. Free tier gets a low cap (50
files per job, 1 GB total) and the page renders an upsell instead of the
drop zone when the quota is exceeded.

The icon should be a folder-with-arrow glyph. Lucide `FolderUp` works.

### Stage 1: drop or pick

A large drop zone, same visual treatment as the existing
`DragDropOverlay` on the dashboard. Two affordances:

- **Drop a folder**: HTML5 `webkitGetAsEntry` recurses, collects all
  audio-extension files (`.m4a .mp3 .wav .flac .ogg .opus .aac .mp4
  .mov .m4v`, same set as the single-file upload accepts), ignores
  everything else, shows a count of accepted and skipped.
- **Pick files**: standard `<input type="file" multiple accept="audio/*,
  video/*">`. No folder recursion, just whatever the user
  multi-selected.

No upload happens at this stage. Files are held in memory as `File`
references; no bytes have left the browser yet. The user can drop
multiple times to accumulate, and can clear the staged set.

### Stage 2: preview table

A scrollable table with one row per staged file. Columns:

- Checkbox (default checked, the user opts a file OUT, not IN)
- Filename (truncated middle, full name on hover)
- Parsed title (editable inline)
- Parsed meeting date (editable inline; native date picker)
- Parsed meeting time (editable inline; native time picker)
- Source (notes / downloads / generic / unknown, read-only badge)
- Confidence (color-coded chip: green ≥0.8, amber 0.5-0.79, red <0.5)
- Size (e.g. "14.7 MB")
- Status (initially "Pending"; transitions during ingest)

The parsed values come from `frontend/src/utils/filenameParser.ts`
running entirely in the browser. No round-trip to the server, no upload.
The user sees the metadata before committing to anything.

Above the table, bulk-edit controls:

- **Apply tag to selected**: free-form, comma-separated, attaches the
  tag list to every checked row's session at create time.
- **Apply title prefix**: e.g. typing "Archive: " prepends to every
  checked row's title.
- **Apply participant**: comma-separated names, parsed into a
  `participants` JSONB list on every checked row. Useful for "all of
  these were calls with Shafen Khan" bulk-mark.
- **Link to project**: project_app + project_id picker (reuses the
  existing `ProjectLinkPicker` component). Optional.

Sort controls on the table header so the user can scan by meeting_date
(default ascending), confidence (lowest first to triage), or size.

A row count + total size summary at the top: "412 files selected /
6.8 GB / oldest 2024-09-14 / newest 2026-05-19".

### Stage 3: confirm

A "Start import" button at the bottom of stage 2. Disabled when zero
rows are checked. Clicking shows a confirmation modal:

> "Import 412 files (6.8 GB)? Files will upload one after another and
> process at 2 in parallel. Estimated completion: ~3 hours. You can
> close this tab during the upload; processing continues server-side."

Confirm creates the `bulk_import_job` row (Section 10), POSTs the file
list metadata to `/api/import/start`, gets back a `job_id`, then
transitions to Stage 4.

### Stage 4: progress

Three regions:

**Header**: job-level summary card.

- Status: Uploading / Processing / Paused / Done / Failed
- Counts: 12 done, 3 failed, 87 in-flight, 310 queued
- Throughput: "12 files / 8.3 min" computed from the last 5 minutes
- ETA: "~2h 47m remaining"
- Buttons: Pause (admin), Cancel (owner or admin), Retry failed (when
  any have failed)

**Live status table**: same row shape as stage 2 preview, but the
status column is now live. Each row goes through:

`Pending → Uploading (NN%) → Processing → Done | Failed`

Updates arrive over a single WebSocket connection scoped to the job_id
(see Section 5). Failed rows show a short error message inline and a
per-row retry button.

**Activity log**: a tail-only event stream at the bottom showing the
last 50 events. Useful when something looks stuck.

The user can leave the page and come back. Reopening `/import/{job_id}`
reconnects to the WS and rehydrates from a GET. Closing the tab does
not stop the job. Closing the laptop does not stop the job.

### Stage 5: completion

When `succeeded + failed == total`, the header banner switches to
"Done. {N} succeeded, {M} failed." Buttons:

- **Open in Sessions**: jumps to the Sessions list, filtered to this
  job's sessions (new `?bulk_import_job_id=...` filter).
- **Retry failed**: requeues just the failed rows.
- **Download report**: CSV of every row with status, session_id (when
  created), and error_message.

The job row stays in the DB indefinitely so the user can revisit
`/import/history` later (a sibling page that lists all bulk_import_jobs
for the active org).

## 4. Backend architecture

### Job queue choice

We need durable per-file work that survives uvicorn restarts and gives
us a single concurrency knob to protect the shared GPU pool. The
candidates:

**FastAPI BackgroundTasks**. The simplest option, but tasks live in the
same uvicorn process. A restart loses every queued job. Already shown to
be inadequate in production: the existing chunked upload pipeline at
`api/uploads.py:UploadPipelineQueue` is an `asyncio.Queue` with `N`
workers, persists job state to Postgres so it can recover from restart,
and that's basically a homegrown queue with all the gotchas. Reusing
that pattern works for one bulk job at a time but doesn't scale to
multi-org concurrency or admin pause/resume.

**Celery + Redis**. Battle-tested, mature ecosystem, multiple brokers,
Flower dashboard. The downsides: Celery is fundamentally sync-first, you
end up running it in async-bridge mode or as a separate non-asyncio
process. It also wants its own broker, result backend, and Beat
scheduler. The operational surface area triples for what is really a
single-purpose feature.

**Arq + Redis**. Modern asyncio-native task queue. Workers run inside an
asyncio event loop, which means we can reuse the same async DB session
pattern the FastAPI app uses, the same `boto3` Garage client, the same
LiteLLM client. Backed by Redis (already in the stack, see
`backend/services/working_audio_service.py` and
`backend/services/agents/brigade.py`). Single config file, in-process
workers, no Beat scheduler unless we add scheduled jobs later. Recent
benchmarks (see [Dquan's LLM notes,
2026-04](https://dangquan1402.github.io/llm-engineering-notes/2026/04/02/lightweight-task-queues-for-llm-apps.html))
put it at 12.5k jobs/sec p99 <200ms vs Celery 3.2k with broker
overhead, for our scale that's irrelevant (we're doing dozens per
hour, not thousands per second) but the operational simplicity matters.

**RQ + Redis**. Similar to Arq, simpler scheduling story, but it's
thread-per-worker sync. We'd lose the asyncio fit with our existing code.

**Recommendation: Arq + Redis** for the production path, with a
**fallback to a Postgres-backed BackgroundTasks queue** during initial
build so we can land the feature behind the eventual Arq migration.
Reasoning:

- We already have Redis in the docker-compose. No new infra.
- The reprocess workers are I/O-bound (waiting on Parakeet HTTP, on
  pyannote HTTP, on LiteLLM HTTP). Asyncio is the right shape.
- The single concurrency knob we need (semaphore-of-2 against the GPU
  pool) is a 5-line Arq pattern.
- We're an asyncio app top-to-bottom. Going sync for one feature is a
  smell.
- Celery's strengths (multi-broker, mature ecosystem, Beat scheduler)
  are things we don't need.

The fallback path: Bulk-import.1 ships using the existing
`UploadPipelineQueue` shape extended with per-org and per-job semaphores
(a Postgres-backed asyncio queue, same recovery semantics the upload
pipeline already has). If Arq turns out to be the wrong call we don't
have to rip it out; we already have the in-process version working.
Bulk-import.4 migrates to Arq if and when we want to fan workers across
hosts.

### High-level flow

```
Browser              FastAPI app                 Arq worker            GPU pool
   |                     |                            |                    |
   | POST /import/start  |                            |                    |
   | (file list metadata)|                            |                    |
   |-------------------->|                            |                    |
   |                     | INSERT bulk_import_jobs    |                    |
   |                     | INSERT bulk_import_files[] |                    |
   |                     | enqueue per-file job ----->|                    |
   |   { job_id }        |                            |                    |
   |<--------------------|                            |                    |
   |                     |                            |                    |
   | POST /import/{job}/files/{file_id}/chunk         |                    |
   | (chunked multipart) |                            |                    |
   |-------------------->|                            |                    |
   |                     | write chunk to disk        |                    |
   |                     | update bulk_import_files   |                    |
   |   { ok }            |                            |                    |
   |<--------------------|                            |                    |
   |                     |                            |                    |
   |   (...repeat per chunk per file, serial...)      |                    |
   |                     |                            |                    |
   | POST /import/{job}/files/{file_id}/finalize      |                    |
   |-------------------->|                            |                    |
   |                     | reassemble, sha256, dedup  |                    |
   |                     | mark file ready ---------->|                    |
   |                     |                            | per-file pipeline: |
   |                     |                            | extract WAV        |
   |                     |                            | create session     |
   |                     |                            | Parakeet --------->|
   |                     |                            |                    |
   |                     |                            | pyannote --------->|
   |                     |                            |                    |
   |                     |                            | Qwen 3.6 -------->|
   |                     |                            | update file row    |
   |                     |                            | push WS event      |
   |                     |                            |                    |
   | WS /ws/import/{job}/progress  <--- per-file deltas + job rollup       |
   |<----------------------------------                                    |
```

### Per-file pipeline

The per-file Arq job executes the following, idempotently:

1. **Pickup**: load the `bulk_import_files` row, verify status is
   `pending` (skip otherwise, the worker may be a redelivery).
2. **SHA-256 + dedup**: hash the assembled audio, query
   `recording_sessions.processing_metadata->>'audio_sha256'` against
   the active org. Match → mark file as `skipped_duplicate`, link to
   the existing session, push WS event, exit.
3. **Filename parse**: `parse_filename(filename)` to recover title /
   meeting_date / meeting_time / source. User overrides from the
   preview table win when present (stored on the file row at
   `/import/start`).
4. **Session row**: insert a `RecordingSession` with the parsed +
   overridden metadata, status=`processing`, source_type=`bulk_import`,
   `processing_metadata.bulk_import_job_id` + `processing_metadata.
   audio_sha256` set. Title flagged `title_user_set=true` if the user
   overrode it in the preview (so the auto-summary doesn't clobber it).
5. **Garage write**: stream the assembled audio to the
   `meeting-ops-audio` Garage bucket under
   `{org_id}/{session_id}/{filename}`. Update
   `recording_sessions.audio_file` with the canonical path or s3 URI
   (depending on what `attachment_storage.write_stream` returns, which
   the existing pattern already standardizes).
6. **Pipeline kick**: reuse the existing `_run_session_reprocess`
   helper (it's what `/finalize-audio` calls). The reprocess pipeline
   already handles Parakeet → pyannote → LLM end to end and writes
   results back to the session row.
7. **Speaker auto-link**: if title matches `Call with {Name}`, attempt
   to match against enrolled speakers in the org (Section 7).
8. **Bookkeeping**: update `bulk_import_files.status='done'`,
   `succeeded_count++` on the job row, push WS event.

Errors at any step set `status='failed'`, populate `error_message`,
increment `failed_count`, push WS event. Tracebacks land in the app
log; the WS payload gets a short user-friendly string only.

### Schema decision: separate `bulk_import_files` table

We considered extending `recording_sessions` with a
`bulk_import_job_id` FK and skipping a second table. Reasons we want
the separate table:

- A bulk import has files that **never become sessions** (skipped
  duplicates, malformed audio, failed transcoding). We need to record
  them somewhere; `recording_sessions` is the wrong home.
- The file row tracks per-row upload state (`bytes_received`,
  `total_size`) that has no business on `recording_sessions`.
- The preview-time overrides (user-edited title / date / time before
  upload starts) need a place to live independent of the session that
  will eventually exist.
- We want a per-job dashboard ("show me everything in this job, even
  the skips and failures"). One join, one query.

Trade-off acknowledged: there's row duplication between
`bulk_import_files` and the session it eventually creates. We keep both
because the file row is the **historical record of the import**, while
the session is the live record of the meeting going forward. If we ever
delete the session, we don't want to lose the audit of where it came
from.

The session row gets a back-pointer: `processing_metadata->>
'bulk_import_job_id'` and `processing_metadata->>'bulk_import_file_id'`,
indexed via the existing JSONB GIN if we end up needing the lookup at
scale. For a few thousand rows it's a full-table scan and that's fine.

## 5. Concurrency model

### Default = 2

Two reprocess slots is the budget. One is held for normal live traffic
(the user who just stopped recording on `/sessions` should never wait
behind a bulk import). One is for batch work. Free, Pro, and Enterprise
all default to this.

Enterprise can override via `Organization.bulk_import_concurrency` (new
column, nullable, defaults to the global) up to a hard cap of 4. Past
4, Parakeet 1.1B starts thrashing on the RTX 6000 and per-file latency
collapses. Hard cap enforced server-side; UI rejects with a clear
error.

### Per-org rate limit

A single Redis sorted set keyed by `import:org:{org_id}:active_files`
holds in-flight file_ids with timestamps. Worker pickup checks the
size; if it would exceed `bulk_import_concurrency` it requeues with
backoff. Stale entries (>10 minutes since a status update) get evicted
by a cron-style sweeper at 30s intervals.

Per-org, not global. A 5000-file org should not starve a 50-file org;
the global GPU pool is protected by per-worker GPU semaphores, not by
per-org caps.

### Adaptive throttle

Each pipeline run records its wall-clock time in
`bulk_import_files.processing_seconds`. The sweeper computes a
rolling p95 over the last 100 completions. If p95 climbs above
180 seconds (2x our expected baseline) the global concurrency drops to
1 until p95 recovers. Logged loudly; admin sees a banner in the
`/import/{job_id}` header.

### Pause + resume

`bulk_import_jobs.status` carries `paused` as a value. Workers on
pickup check the status and if `paused` they requeue with a 30s delay.
The UI exposes Pause and Resume buttons to org admins. Useful when
something else is happening on the box (a board meeting going live,
a video rendering, a release deploy) and you want to stop slamming GPU
for a few minutes.

## 6. Progress UI

### Primary: WebSocket

Endpoint: `GET /ws/import/{job_id}/progress`. One connection per open
`/import/{job_id}` page. Server pushes:

```jsonc
// Initial frame on connect
{
  "type": "snapshot",
  "job": { "id": "...", "status": "processing", "total_files": 412,
           "succeeded": 12, "failed": 1, "in_flight": 4, "queued": 395 },
  "files": [/* recent N file rows */]
}
// Per-file delta
{
  "type": "file_update",
  "file_id": "...",
  "status": "processing",
  "session_id": 1834,
  "progress_pct": 45,
  "error_message": null
}
// Per-job rollup (every 5s or on any file transition)
{
  "type": "job_rollup",
  "succeeded": 13,
  "failed": 1,
  "in_flight": 3,
  "queued": 395,
  "throughput_per_min": 1.4,
  "eta_seconds": 9876
}
```

Auth: same Keycloak-issued JWT the rest of the app uses, validated on
connect, org-scoped check that the user owns or shares the job.

The existing `UploadWebSocketManager` pattern in `api/uploads.py` is the
template. Same per-id connection set, same broadcast helper, same
disconnect handling.

### Fallback: Server-Sent Events

Endpoint: `GET /api/import/{job_id}/progress/stream`. Same payload
shape, one event per delta, `event: file_update` / `event: job_rollup`.
We don't expect to ever fall through to this in production, but
oauth2-proxy in front of WebSockets has a history of breaking under
certain TLS termination configs and SSE is the next-most-real-time
fallback.

### Final fallback: polling

`GET /api/import/jobs/{job_id}` returns the same snapshot shape. UI
falls back to a 5-second poll if both WS and SSE fail to connect. Loud
console warning in dev so we notice immediately when production breaks.

## 7. Error handling

### Per-file failures

Each file's pipeline runs inside a single try/except in the Arq job.
Failures set `status='failed'` with a short error_message string. The
user sees the message inline in the progress table. Retry-failed is a
single button on the job header that requeues every failed file with
the same overrides.

Common error categories the UI surfaces with friendly messages:

- `ffmpeg_failed`: "Audio extraction failed. The file may be
  corrupted."
- `transcription_failed`: "Transcription timed out. Try again or
  contact support."
- `garage_upload_failed`: "Storage upload failed. Will retry
  automatically."
- `quota_exceeded`: "Your organization has hit its monthly hours
  cap. Upgrade to continue."
- `duplicate_skipped`: not an error, green badge, "Already imported on
  2025-11-04."

### Mid-stream upload cancel

A user closing the tab during chunk upload terminates the multipart
stream. The chunks already on disk persist. The orphan file row stays
in `uploading` status until the cleanup sweeper marks it `failed` after
30 minutes of inactivity. Reopening `/import/{job_id}` shows the file
in failed state; Retry will re-stream from chunk 0.

A user clicking Cancel on the job row sets `bulk_import_jobs.status=
'cancelled'`. Workers on pickup check the parent job; if cancelled,
they skip the file and mark it `cancelled`. Files already in-flight
finish naturally, we don't kill mid-Parakeet. The job ends when the
last in-flight file completes.

### Server restart mid-batch

The Arq worker is in-process with FastAPI; a uvicorn restart kills it.
Recovery on startup:

1. Lifespan hook scans `bulk_import_jobs.status='processing'` and
   `bulk_import_files.status IN ('uploading','queued','processing')`.
2. Files in `uploading` with no chunks on disk in the last 5 minutes
   are marked `failed` (the client gave up).
3. Files in `queued` are re-enqueued in the Arq queue.
4. Files in `processing` are inspected: if the underlying
   `recording_sessions` row exists and is `completed`, mark the file
   `done`; otherwise re-enqueue as `queued`.

Same pattern the existing upload pipeline uses
(`cleanup_stale_uploads`).

### Resume after cancel

`PATCH /api/import/jobs/{job_id}` with `{"status":"resuming"}` flips
the job back to `processing` and re-enqueues every file in `cancelled`
or `failed` state. Files in `done`, `skipped_duplicate`, or
`uploading` are left alone.

### Duplicate detection

SHA-256 of the assembled audio is computed once at the end of upload
(streaming, via `hashlib` chunked over the temp file). It is checked
against `recording_sessions.processing_metadata->>'audio_sha256'`
scoped to the org. Match → file row goes to `skipped_duplicate` with
a link to the existing session, no GPU time spent, no session created.

Cross-org duplicate detection is deliberately not done. Two orgs may
legitimately upload the same recording (a podcast guest sharing audio
with both the host and their own org) and we should not surface that
fact across tenant boundaries.

The hash is recorded on the session row too
(`processing_metadata.audio_sha256`) so subsequent single-file uploads
also hit the dedup check. That's a backfill task: a one-time job over
the existing `recording_sessions` table to populate `audio_sha256`
where the audio is still on disk. Cheap; ~30 minutes for the current
catalog.

## 8. Speaker auto-link from "Call with X"

The Notes export pattern is overwhelmingly `Call with {Person}` or
`{Person} call` for one-on-ones. We can use this as a hint to pre-link
the session to a known speaker before diarization runs.

### Match logic

After session creation, before pipeline kick:

1. Parse the title against `Call with (.+)` (case-insensitive, also
   match `(.+) call`, `Conversation with (.+)`, `Meeting with (.+)`).
2. Normalize the captured name (strip punctuation, lowercase).
3. Query `SpeakerProfile` in the active org for exact match on
   normalized `display_name`. If no exact match, fuzzy-match using
   Postgres `pg_trgm` similarity > 0.85.
4. On match: create a `SpeakerSessionLink` row with
   `source='filename-hint'`, `raw_label='HINT'` (a sentinel), and
   `confirmed=false`.

The `source='filename-hint'` value is **new**, slotting between the
existing `source='auto'` (embedding match from pyannote) and
`source='manual'` (user-clicked). The
`SpeakerSessionLink.source` column today only carries `auto` or
`manual` per the model definition; we extend the allowed values
without a schema change (column is a free-form `String(20)`).

### Precedence

When the reprocess pipeline finishes diarization and runs
`identify_speakers`, the embedding-match has the canonical word. The
filename hint either:

- **Agrees** with the embedding match (same speaker_id): mark the
  filename-hint row as confirmed=true, source upgraded to `auto`.
- **Disagrees**: the embedding match wins. The filename-hint row is
  deleted. We log the disagreement at INFO with both speaker_ids for
  later analysis of how reliable the filename pattern actually is.
- **No embedding match** (diarization didn't recognize the speaker,
  e.g. brand-new voice): the filename hint becomes the displayed
  attribution. The session shows "Speaker 0 likely is {Name} (from
  filename)" in the UI with a low-confidence badge.

This means the filename hint is **never trusted blindly**. It seeds the
UI with a useful guess that the embedding match either confirms or
corrects.

### Edge cases

- Title says "Call with Jason" but the org has both "Jason Allen" and
  "Jason Patel": the fuzzy match returns the higher-similarity row.
  If both are equally similar (tie at 1.0), we skip the hint and
  log it as ambiguous. UI shows no pre-link.
- Title says "Call with Mom": no SpeakerProfile match. We do NOT
  create a SpeakerProfile from the hint, that would pollute the org's
  speaker library with bulk-import garbage. The hint is a no-op when
  there's no existing profile to link to.
- Title says "Call with Shafen" but pyannote reports two speakers (the
  user and Shafen): the hint links to Shafen's profile; the user's
  speaker_id (also enrolled) gets identified by the normal embedding
  match. Both end up correctly attributed.

## 9. Storage layout

### Bucket

A new bucket `meeting-ops-audio` on the existing Garage cluster. We do
**not** reuse `meeting-ops-attachments` (the attachments bucket holds
PDFs, slide decks, transcripts, different lifecycle, different access
pattern, different per-org quota math). Bucket creation is an ops task
(garage-cli `bucket create`), tracked in
`Unicorn-Ecosystem/garage/buckets.md`.

Same boto3 path-style client config the attachment_storage module
already uses. Endpoint URL + access key + secret pulled from
`GARAGE_*` env vars.

### Object key format

```
{org_id}/{session_id}/{filename}
```

Identical to attachment_storage's convention. session_id is the
integer PK, not the UUID `session_id` column, because we generate the
PK first and then write the audio. filename is sanitized (control
chars stripped, length capped at 240, path separators removed).

A possible future iteration is to layer a date prefix
(`{org_id}/{YYYY}/{MM}/{session_id}/...`) for cold-storage lifecycle
rules, but Garage doesn't currently support S3 lifecycle policies and
we have no immediate need. Leave it flat.

### Retention

- **Successful imports**: audio file retained per the org's general
  audio retention policy (today: indefinite for Pro+, 30 days for
  Free).
- **Failed imports** (file failed pipeline but bytes are on disk):
  retained 7 days, then a cron sweeper removes the bytes and marks the
  file row `expired_audio`. The row stays, so the user sees what
  happened.
- **Cancelled imports**: bytes are deleted immediately when the user
  cancels.
- **Duplicate-skipped imports**: bytes never land in the bucket. The
  upload is reassembled on local disk for hashing, then deleted as
  soon as the dedup check hits.

### Encryption

Garage handles at-rest encryption with cluster-level keys. We don't
layer application-level encryption on top. The bucket sits behind a
private network ACL (only the docker network can reach
`unicorn-garage:3900`); no public ingress.

## 10. Database schema

Two new tables. Postgres-only production target. SQLite test fixture
takes the same DDL with `JSONB → JSON` and `UUID → CHAR(36)`
substitutions handled by SQLAlchemy.

### `bulk_import_jobs`

```sql
CREATE TABLE bulk_import_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | uploading | processing | paused | done | cancelled | failed
    total_files     INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    total_bytes     BIGINT NOT NULL DEFAULT 0,
    bytes_received  BIGINT NOT NULL DEFAULT 0,
    concurrency     INTEGER NOT NULL DEFAULT 2,
    error_message   TEXT,
    started_at      TIMESTAMP WITH TIME ZONE,
    finished_at     TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_bulk_import_jobs_org_status_created
    ON bulk_import_jobs (organization_id, status, created_at DESC);
CREATE INDEX ix_bulk_import_jobs_job_id
    ON bulk_import_jobs (job_id);
```

### `bulk_import_files`

```sql
CREATE TABLE bulk_import_files (
    id                BIGSERIAL PRIMARY KEY,
    file_id           UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    job_id            UUID NOT NULL REFERENCES bulk_import_jobs(job_id) ON DELETE CASCADE,
    organization_id   INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    filename          TEXT NOT NULL,
    content_type      TEXT,
    total_size        BIGINT NOT NULL,
    bytes_received    BIGINT NOT NULL DEFAULT 0,
    chunks_received   INTEGER NOT NULL DEFAULT 0,
    total_chunks      INTEGER NOT NULL DEFAULT 0,
    audio_sha256      VARCHAR(64),
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | uploading | queued | processing | done | failed | cancelled | skipped_duplicate | expired_audio
    parsed_title      TEXT,
    parsed_meeting_date DATE,
    parsed_meeting_time TIME,
    parsed_source     VARCHAR(20),
    parsed_confidence REAL,
    override_title    TEXT,
    override_meeting_date DATE,
    override_meeting_time TIME,
    override_tags     TEXT[] NOT NULL DEFAULT '{}',
    override_participants JSONB NOT NULL DEFAULT '[]',
    override_project_app  VARCHAR(50),
    override_project_id   TEXT,
    session_id        INTEGER REFERENCES recording_sessions(id) ON DELETE SET NULL,
    duplicate_of_session_id INTEGER REFERENCES recording_sessions(id) ON DELETE SET NULL,
    error_message     TEXT,
    processing_seconds REAL,
    created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_bulk_import_files_job_status
    ON bulk_import_files (job_id, status);
CREATE INDEX ix_bulk_import_files_org_created
    ON bulk_import_files (organization_id, created_at DESC);
CREATE INDEX ix_bulk_import_files_file_id
    ON bulk_import_files (file_id);
CREATE INDEX ix_bulk_import_files_session
    ON bulk_import_files (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX ix_bulk_import_files_sha256
    ON bulk_import_files (organization_id, audio_sha256) WHERE audio_sha256 IS NOT NULL;
```

The CASCADE on `job_id` lets us clean up a job and all its file rows in
one delete. SET NULL on `session_id` and `duplicate_of_session_id`
preserves the import audit history even if the session is later
deleted.

### Alembic migration sketch

```python
# 028_bulk_import_jobs.py
revision = "028_bulk_import_jobs"
down_revision = "027_meeting_date_time"

def upgrade():
    op.create_table(
        "bulk_import_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"),
                  nullable=False, unique=True),
        # ...
    )
    op.create_index(...)

    op.create_table("bulk_import_files", ...)
    op.create_index(...)

def downgrade():
    op.drop_table("bulk_import_files")
    op.drop_table("bulk_import_jobs")
```

We also add an indexed JSONB lookup on the session side:

```sql
CREATE INDEX ix_recording_sessions_audio_sha256
    ON recording_sessions ((processing_metadata->>'audio_sha256'))
    WHERE processing_metadata->>'audio_sha256' IS NOT NULL;
```

So the dedup check hits an index instead of a table scan.

## 11. Security

### File-size limits

- Per-file: 200 MB default, 2 GB Pro, 10 GB Enterprise. Inherited from
  the existing `services.quotas.TierLimits.max_file_bytes`. Bulk import
  reuses the same check at file finalize.
- Per-job: 10 GB Free, 50 GB Pro, 500 GB Enterprise. New tier setting
  `max_bulk_import_bytes_per_job`. Reasoning: caps the blast radius
  of any one job both for storage and for processing cost.
- Per-org per-day: rolling 24h cap on bytes ingested via bulk import.
  Default 50 GB Free, 200 GB Pro, unlimited Enterprise. Prevents an
  org from repeatedly creating and re-creating bulk jobs to evade the
  per-job cap.

### MIME / extension validation

Accept-list of audio extensions only (same set the single-file upload
accepts). Files outside the list are silently dropped client-side
during folder selection; if they slip through to the server, we reject
at finalize with `415 Unsupported Media Type`.

`UploadFile.content_type` is **untrusted**. We run a real probe with
`ffprobe -show_streams` server-side at finalize to verify the file
parses as audio (or video, we extract audio from video too). Probe
failure → reject with `400` and the file row goes to `failed` with
error_message="Not a valid audio file".

### Filename sanitization

Reuse `_safe_filename` from `api/uploads.py`: strip null bytes, strip
path traversal characters, replace control chars with `_`, cap length
at 240 chars. The sanitized filename is what we store in
`bulk_import_files.filename` and in the Garage object key.

The display name on the preview table is the **original** filename
(unsanitized), so the user sees what they uploaded. The sanitized
version is what hits the filesystem and the bucket.

### Auth + RBAC

- `/import` page: any authenticated user in any org.
- `POST /api/import/start`: standard org-scoped auth (the user must be
  a member of the active org).
- `PATCH /api/import/jobs/{job_id}` (pause/resume/cancel): the job
  owner (`user_id` match) or an org admin.
- `GET /api/import/jobs/...`: org admins see all jobs in the org; non-
  admins see only their own.
- WS `/ws/import/{job_id}/progress`: same auth as the GET.

### Per-org quota enforcement

Bulk import goes through the same `services.quotas.check_upload_quota`
function as single-file uploads (per-file size cap, per-org concurrent
uploads, monthly hours). It also runs the new per-job and per-day
bulk-specific caps at `/api/import/start`. If the user is over any
cap, the start request fails with `402` (payment required) and a
structured detail payload that the frontend renders as an upsell.

## 12. Implementation plan

### Bulk-import.1 (backend, ~1.5 days)

Alembic 028 for the two new tables. New `backend/api/import_jobs.py`
router with:

- `POST /api/import/start`, accepts file metadata list, creates the
  job + file rows, returns `job_id`.
- `POST /api/import/{job_id}/files/{file_id}/chunk`, chunked
  multipart upload, identical to the existing
  `/api/uploads/{upload_id}/chunk` shape.
- `POST /api/import/{job_id}/files/{file_id}/finalize`, reassemble,
  SHA-256, dedup, write to Garage, enqueue per-file job.
- `GET /api/import/jobs/{job_id}`, snapshot.
- `GET /api/import/jobs`, list (org-scoped, paginated).
- `PATCH /api/import/jobs/{job_id}`, pause / resume / cancel.

New `backend/services/bulk_import_pipeline.py` with the per-file
pipeline as an async function. Initial worker runs inside FastAPI as
an asyncio task pool with a 2-slot semaphore (same pattern
`UploadPipelineQueue` uses); Arq migration deferred to Bulk-import.4.

Tests: unit on the pipeline, HTTP-level on the routes, cross-org leak
case in the existing `test_cross_org_*.py` style.

### Bulk-import.2 (frontend, ~1.5 days)

New `frontend/src/pages/Import.tsx` with the staged UX (drop → preview
→ confirm → progress → done). Hooks for WS connect + reconnect +
fallback to SSE then polling. Sidebar entry. Bulk-edit controls in the
preview table reuse existing components (`ProjectLinkPicker`, tag
input, etc.).

Mobile is read-only on day one (you can monitor a job from your phone,
but you can't drop a folder there because folder pick doesn't exist on
mobile browsers). Desktop is the target. The Mac Notes archive is on a
Mac; we're optimizing for that.

Tests: a Playwright run that drops a small fixture of 5 files, walks
the preview, starts the import, asserts progress events flow, asserts
sessions exist at the end.

### Bulk-import.3 (speaker auto-link + retry, ~0.5 days)

Implement the `Call with X` matcher. Extend the existing
`identify_speakers` post-pipeline pass to upgrade or evict the
filename-hint row based on embedding agreement.

Retry-failed button on the job header. Per-row retry button. Both
endpoints already exist (the existing `/uploads/{id}/retry` is the
template).

### Bulk-import.4 (Arq + admin controls + observability, ~1 day)

Migrate the worker pool from the in-process asyncio queue to Arq
backed by Redis. Same per-file pipeline function; we're just changing
where it's enqueued.

Admin controls: pause/resume buttons in the UI, the per-org
concurrency override on the Organization page, an `/admin/import`
overview page that lists every active job in the deployment with a
kill switch.

Observability: Prometheus counters for jobs by status, files by
status, processing_seconds histogram, dedup hit rate. Grafana panel
in the meeting-ops dashboard. Loki alert when failed_count grows
faster than succeeded_count over a 5-minute window.

### Total

~4.5 days end-to-end. Add a half day for testing against the actual
526-file corpus and tightening based on what surfaces in real use →
**5 working days budget**.

## 13. Open questions

These need Aaron's input before Bulk-import.1 lands.

1. **Arq vs in-process for v1**. The recommendation is in-process for
   v1, Arq migration in v4. Alternative is to go straight to Arq in
   v1 and skip the migration. Trade-off: in-process is faster to build
   and faster to debug; Arq is the right long-term home. Aaron's call.

2. **Free-tier ceiling**. The proposal is 50 files per job, 1 GB per
   job. That's enough for "upload a week of voice memos" but not
   enough to backfill a year. Is Free meant to be a real evaluation
   tier (in which case 50 files is too few) or a marketing tier (in
   which case 50 files is correct)? This ties into tier pricing more
   broadly.

3. **Per-org concurrency cap**. The proposal is 2 default, 4
   Enterprise hard cap. Beyond 4, Parakeet 1.1B starts thrashing the
   RTX 6000. Is 4 the right ceiling, or do we want a higher ceiling
   with admin pause as the safety valve? Aaron is the only org that
   would hit this in 2026 so the answer is "what Aaron wants for the
   526-file corpus".

4. **Filename-hint speaker pre-link: opt-in or default-on?** The
   proposal is default-on with the embedding match as the canonical
   override. Alternative is opt-in (a checkbox in the preview table:
   "Use filename to suggest speakers"). Default-on is the better UX
   if the agreement rate is high in practice; opt-in is safer if we
   end up with frequent disagreements polluting the speaker library.
   We can ship default-on and track the agreement rate in the first
   week; if it's bad, we flip to opt-in.

5. **Retention of intermediate files**. The proposal is 7 days for
   failed-upload artifacts, immediate delete on cancel. Should
   successful imports keep their pre-Garage local copies for some
   period (rollback aid) or delete immediately on Garage write
   confirmation? Immediate delete is the disk-cheaper choice; 24h
   retention is the rollback-safer choice.

6. **Resume from the same `/import` page on a different browser**.
   Aaron drops the folder on his Mac, the upload starts, he closes
   the laptop. The job continues server-side. He reopens `/import`
   on his desktop. Should the open job auto-populate the page, or
   should he have to navigate to `/import/{job_id}` via the history
   list? Auto-populate is the friendlier UX; surfacing the job in the
   sidebar (a small badge) is even better. Worth a design decision.

7. **Reuse `/finalize-audio` or dedicated bulk-upload endpoint?** The
   proposal is dedicated endpoints for clarity (`/api/import/...`)
   even though the per-file work overlaps heavily with what
   `/finalize-audio` does. Alternative is to reuse `/finalize-audio`
   and just pass a `bulk_import_file_id` header. Dedicated endpoints
   make the API surface clearer; reuse is DRYer. The recommendation
   is dedicated, but if Aaron wants to keep the endpoint surface
   small we can do reuse.

## 14. Future work

- **Drag-from-Finder direct**: macOS-specific webview integration so
  the user can drag from Finder without going through the standard
  file picker. Pure polish.
- **Resume partial uploads across browsers**: today, the chunks-on-
  disk only get matched up if you reload the same browser session
  (the upload_id is in localStorage). A cross-browser resume would
  need the file row to track a client-stable identifier (e.g. the
  filename + size + first-1KB-hash) so a different browser can pick
  up where the first left off. Not a v1 requirement.
- **Bulk export**: the inverse operation. Pick 526 sessions, get a
  zip of their audio + transcripts + summaries. Reuses the same job
  table shape with `direction='export'`.
- **Folder-organized session display**: ingest a folder tree
  (`2024/09/...`, `2024/10/...`) and preserve the hierarchy in the
  Sessions view. Aaron's corpus is flat so this isn't a v1 need.
- **Per-import LLM template override**: bulk-summarize the entire
  archive with a custom prompt ("These are all calls with the same
  person, extract every project name mentioned across all 526
  meetings"). Slot fits cleanly under the existing
  `summary_template` per-upload preference.

## 15. Out of scope

- **Cross-tenant dedup**: not done; two orgs uploading the same
  audio file is treated as two separate ingests. See Section 7.
- **Live transcription during bulk import**: bulk imports are
  always post-processed. No streaming.
- **Browser-side STT for bulk imports**: even on a desktop-capable
  device, we route bulk through the server pipeline for speed and
  consistency. The Mac's CPU does not get to be a transcription
  worker.
- **Auto-categorization beyond filename**: we use the filename
  parser plus user-supplied overrides. We do not run an LLM "guess
  the meeting type" pass at ingest. The post-pipeline summary
  surfaces meeting type naturally.
- **External source integrations**: Zoom cloud recordings, Granola
  exports, Otter exports. Different ingest shapes; different design
  doc when we get to them. The bulk-import job + file tables are
  general enough to host them, but the per-file pipeline is
  audio-file-specific in v1.

## 16. Risks

- **GPU pool contention**: the biggest risk. A 526-file job at
  concurrency=2 occupies one Parakeet slot for hours; live users
  compete for the other. Worst case is a live user waiting 180s
  instead of 90s for a transcript, acceptable. Catastrophic case is
  the GPU OOMing, not acceptable. The adaptive throttle drops
  concurrency to 1 when p95 latency degrades, which is the guard.

- **Garage bucket growth**: 8 GB for Aaron's corpus is nothing, but
  larger imports could hit the cluster ceiling. Operational guard is
  the per-org per-day cap; cluster guard is a Grafana panel with an
  80% alert.

- **Filename parser regression**: if Apple changes the Notes export
  shape, every fresh corpus drops to confidence 0.0 and the user
  overrides every row. CI smoke test asserts the canonical sample
  parses; in place from `e447cf9`.

- **Cross-tab overlap**: two tabs starting imports of the same files
  could create duplicate jobs. Mitigation: a uniqueness constraint on
  `(organization_id, filename, total_size, audio_sha256)` for files
  created within the same minute window, the second `start` fails
  with a clear message.

## 17. End-to-end estimate for the corpus

526 files, ~8 GB, concurrency=2:

- Upload time: ~30-45 minutes on a typical home connection. The
  uploads are serial from the browser side (one file at a time over
  the chunked endpoint) so this is a hard floor.
- Processing time: ~5-9 hours. Two parallel reprocess slots, each
  averaging 30-90 seconds per file (the variance is meeting length;
  most of Aaron's voice memos are 5-30 minutes).
- Wall clock: dominated by GPU. Upload finishes long before the queue
  empties.

The user can close the tab after upload. Reopening `/import/{job_id}`
the next morning shows a completed job with 526 new sessions.
