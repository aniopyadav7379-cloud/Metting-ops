# Inference pipeline: concurrent diarization + 24k-slot summary tuning (2026-07-02)

This note documents the server-side processing pipeline for an uploaded/reprocessed
meeting, the GPU routing behind it, and two changes shipped on 2026-07-02:
STT‖diarization concurrency and the summary chunk/geometry tuning.

## Pipeline stages (`api/recording.py::_run_session_reprocess`)

1. **Resolve audio** — reassemble always-on chunks, or use the uploaded file.
2. **Transcription (STT)** — Parakeet 1.1B (`meet-parakeet-svc`, 4070/midboy1).
3. **Diarization + fingerprint** — pyannote 3.1 (`meet-speaker-svc`, P40/midboy2),
   returns per-segment embeddings; overlaid onto the transcript segments.
4. **Speaker identification** — match embeddings to enrolled `SpeakerProfile`s.
5. **Summary** — map-reduce or single-call against the summarizer LLM.

### Change 1 — diarization runs concurrently with transcription

Stages 2 and 3 read the **same audio independently**, on **different GPUs**, and
are only merged afterward (diarization labels are overlaid onto transcript
segments). They previously ran serially (`await` STT, then `await` diarize).

They now overlap: the diarization provider is set up (a synchronous DB read) and
`diarize()` is launched as an `asyncio.create_task(...)` **before** the STT await,
then awaited in Stage 3. Wall-clock for that phase drops from `STT + diarize` to
~`max(STT, diarize)`.

Safety:
- The provider's DB setup completes before the parallel section; `diarize()` is
  HTTP-only (no DB), so it cannot race the shared (synchronous) SQLAlchemy session.
- If STT raises, the diarize task is cancelled (no orphaned pending task).
- If diarization is unavailable, the task is `None` and the pipeline degrades
  cleanly to transcript-only — unchanged behavior.

Verified on a 76-minute meeting: diarization started ~5s into the reprocess and
overlapped ~43s of transcription; result unchanged (transcript + 2 speakers +
speaker-turns + summary).

## GPU routing (as of 2026-07-02)

| Stage | Service | GPU | Endpoint |
|-------|---------|-----|----------|
| STT | `meet-parakeet-svc` | 4070 (midboy1) | `:8881` |
| Diarize + fingerprint | `meet-speaker-svc` | P40 (midboy2) | `:8889` |
| Summary / chat (dev) | `vision-6000` | RTX 6000 (bigboy) | `llm-gateway:8088` |
| Summary (prod) | `vision-6000` via midboy1 vision-forward | RTX 6000 | `llm-gateway:8088` |
| Embeddings / rerank | infinity | 3060 (midboy2) via `unicorn-infinity-proxy:8080` | proxy |

The summarizer model on `vision-6000` is a single loaded Qwen 3.6 35B-A3B (+ vision
mmproj) instance that serves **both** vision requests (item-listing, etc.) and
text summaries. It runs `--ctx-size 98304 -np 4`, i.e. **4 continuous-batch slots
of 24,576 tokens each**. `-np 4` (vs 3) guarantees the 3-wide summary map-reduce
never occupies every slot, so a co-tenant vision workload always has ≥1 slot free.

### Change 2 — summary tuning for the 24k slot

With a 24,576-token slot the binding constraint is the **single-call** summary path
(`max_tokens=8192` output, plus the transcript up to the direct-route cap of
~50k tokens). To keep every path inside 24k:

- `MEETING_OPS_SUMMARY_CHUNK_TOKENS=14000` — a map call is `chunk (14k) + prompt
  (~2k) + output (4k) ≈ 20k`; a single call is `transcript (≤14k) + output (8k) ≈
  23k`. Both fit with margin.
- `MEETING_OPS_SUMMARY_MAPREDUCE=1` — long meetings are chunked (full coverage)
  instead of single-call-truncated at the transcript cap.

These are env-driven and now passed through in `deploy/unicorncommander`.

## Notes

- llama.cpp / llama-swap **404 instantly** on a model alias the endpoint doesn't
  serve (e.g. `-Vision` at the P40 which only serves `-p40`). A fast 404 at the
  summarizer means a stale model name, not an outage.
- A stuck session whose summary failed keeps its `processing_job_id`; the finalize
  drift-guard then skips new jobs. To reprocess: clear `processing_job_id` and
  re-enqueue `finalize_session_job` / `reprocess_session_job` on the interactive
  queue.
- The reprocess pipeline imports all ORM models via the worker; running
  `_run_session_reprocess` from a bare `python -c` fails on an unresolved
  `conference_rooms` FK — test by enqueuing onto the running worker instead.
