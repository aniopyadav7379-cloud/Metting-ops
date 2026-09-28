# Audio storage in Garage object storage

Status: **live (cutover complete, v3.9.0)**. Last updated: 2026-05-27.

How Meeting-Ops stores the canonical meeting audio, why, and how to operate it.

## Summary

The main meeting audio used to live only on a single host's local disk. As of
v3.7.0 it has a durable copy in **Garage** (S3-compatible object storage on our
own hardware), mirroring how session attachments already work. As of the v3.9.0
**cutover**, Garage is the authoritative home and local disk is a transient
working cache.

- **Durable canonical store:** Garage bucket `meeting-ops-audio`, key convention
  `{org_id}/{session_id}/audio/{filename}`.
- **Local disk:** working copy during processing + an evictable cache for reads.
- **Recorded on the session row:** `recording_sessions.audio_storage_backend`
  (`garage` | `local` | NULL) and `recording_sessions.audio_object_key`. The
  legacy `audio_file` column stays the local path. NULL backend = not yet pushed
  (read paths fall back to the local file), so pre-existing rows kept working.
- **Free-tier audio is exempt** — it stays in the browser (IndexedDB) and never
  reaches the server, so it never reaches Garage.

This is *not* a third-party cloud. The audio never leaves our infrastructure,
which keeps the privacy story intact (and is a HIPAA/enterprise selling point)
while costing only disk — no per-GB cloud fees.

## What goes where

| Artifact | Home |
|---|---|
| Canonical meeting audio (reassembled / uploaded / bulk-imported) | **Garage** (durable) + local cache |
| Always-on raw chunks | local transient (reassembled, then cleaned) |
| Session attachments | Garage (`meeting-ops-attachments`, separate module) |
| Transcripts, summaries, diarized segments, speaker embeddings | PostgreSQL |
| TTS outputs | local only (regenerable; Garage deferred) |
| Free-tier audio | browser IndexedDB only |

## Code

- `backend/services/media_storage.py` — storage-only Garage client for the
  audio bucket: `put_path` / `put_stream` / `open_object` / `iter_object` /
  `cached_local_path` / `delete_object` / `delete_prefix`, a local-first spool
  fallback, and the `MEDIA_STORAGE_DISABLED` escape hatch (tests force-local).
- `backend/services/session_media.py` — session-row glue:
  - `persist_session_audio(db, session, local_path=...)` — best-effort push to
    Garage + record the columns. Never raises; a Garage hiccup leaves the
    columns NULL to retry. Wired into always-on finalize (`api/recording.py`),
    both upload paths (`api/uploads.py`), and bulk-import
    (`services/bulk_import_queue.py`).
  - `resolve_local_path(session)` — local-first, else pull from Garage into the
    cache. Used by `download/audio` (`api/simple_recording_db.py`) and the
    `identify_speakers` re-extraction fallback (`services/speaker_service.py`).
  - `purge_session_media(session)` — delete the whole `{org}/{session_id}/`
    prefix on session delete (GDPR/CCPA "delete my data").
- Migration `backend/alembic/versions/031_session_audio_object_storage.py`.

## Reads, writes, deletes

- **Write:** the pipeline writes the file to local disk (ffmpeg/STT/diarize need
  a real file), then `persist_session_audio` pushes a copy to Garage and records
  the columns. Best-effort + additive.
- **Read (download/playback):** `resolve_local_path` returns the local file if
  present, else downloads from Garage into the media-cache and serves that. The
  backend always proxies the bytes (range-capable `FileResponse`); Garage is
  never exposed to the browser.
- **Delete:** `purge_session_media` removes every Garage object under the
  session prefix plus local cache copies.

## Operations

Scripts run inside the `meet-backend` container on bigboy.

```bash
# Backfill: push existing local audio into Garage + record columns (idempotent)
docker exec meet-backend python3 scripts/backfill_audio_to_garage.py --dry-run
docker exec meet-backend python3 scripts/backfill_audio_to_garage.py

# Cutover eviction: delete local copies that have a size-verified Garage object
docker exec meet-backend python3 scripts/evict_local_audio.py --dry-run
docker exec meet-backend python3 scripts/evict_local_audio.py
docker exec meet-backend python3 scripts/evict_local_audio.py --keep-days 7   # keep recent local warm
```

`evict_local_audio.py` never deletes a local file without first confirming the
Garage object exists with a matching byte size (verified via list, not HEAD).
It is idempotent and reversible — the bytes remain in Garage and re-materialize
into the cache on next access.

### Provisioning the bucket (one-time, out of band)

The `meetingops` Garage data key cannot create buckets. Provision via the
Garage admin binary (the `unicorn-garage` container is distroless — invoke
`/garage` directly):

```bash
docker exec unicorn-garage /garage bucket create meeting-ops-audio
docker exec unicorn-garage /garage bucket allow --read --write --owner meeting-ops-audio --key meetingops
```

### Garage gotcha: HEAD returns 400

Garage v1.0.1 returns **400 on HEAD** (`head_object` / `head_bucket`) with our
botocore (same family as the `@aws-sdk` checksum/HEAD issues). `GET`/`PUT` are
fine. So `cached_local_path` streams via `get_object` (not `download_file`,
which HEADs first) and `ensure_bucket` does not probe with `head_bucket`. Verify
object presence/size with `list_objects_v2`, not HEAD.

## Environment

- `GARAGE_ENDPOINT_URL`, `GARAGE_ACCESS_KEY`, `GARAGE_SECRET_KEY`, `GARAGE_REGION`
  (shared with attachments).
- `GARAGE_AUDIO_BUCKET` (default `meeting-ops-audio`).
- `MEDIA_CACHE_DIR` (default `RECORDINGS_DIR/media-cache`).
- `MEDIA_STORAGE_DISABLED=1` forces the local backend (used by tests).

## Not yet (follow-ups)

- LRU eviction for the re-materialization cache (fine at current volume; the
  cache grows as evicted audio is replayed).
- A scheduled retention/eviction job (eviction is manual today).
- Per-tier retention/lifecycle policy and encryption-at-rest (HIPAA path).
- TTS-output durability (currently local-only, regenerable).
