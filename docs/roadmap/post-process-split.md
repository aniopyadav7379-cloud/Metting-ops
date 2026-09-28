# Roadmap: post-process split into separate meetings

**Status**: Backlog / v3.24+
**Owner**: Aaron's call when to schedule
**Author**: Claude (Opus 4.7)
**Date filed**: 2026-06-02
**Triggered by**: v3.23.1 silence-split bug — single 31-min recording fragmented into 6 sessions because the in-recording VAD engine treated normal thinking pauses as meeting boundaries

---

## The decision

**Don't auto-split during recording.** Auto-split is a post-processing decision made AFTER the recording is complete, in the session detail screen, **user-initiated by default**.

In-recording silence-based session splitting is disabled in v3.23.2 (`silenceThresholdMs: Number.MAX_SAFE_INTEGER` in `frontend/src/contexts/AlwaysOnContext.tsx`). The full post-process feature replaces it.

## Why this design is correct

1. **The audio file stays whole** — one continuous recording, easy to scrub, easy to verify splits visually
2. **Splits are computed with global context** — the system sees the entire transcript and can find natural break points instead of reacting to local silence
3. **User can review and adjust** — see proposed split points, drag the timeline marker, name each child meeting, cancel
4. **No false positives** — a thinking pause during a single meeting never forces a new meeting boundary
5. **Reversibility** — can undo a split if the user changes their mind (children → parent reunify)

## Scope when this lands

### Backend

- New column `recording_sessions.parent_session_id` (UUID, nullable, FK to recording_sessions.id)
- New column `recording_sessions.is_split_parent` (boolean, default false)
- New endpoint `GET /api/simple/recording-sessions/{id}/proposed-splits`:
  ```json
  {
    "session_id": "...",
    "proposed_splits": [
      {
        "at_seconds": 1845.2,
        "silence_ms": 360000,
        "preceding_text_preview": "...so we'll table that for next week.",
        "following_text_preview": "Alright everyone, welcome to the budget review meeting...",
        "confidence": 0.92
      }
    ],
    "suggested_min_silence_ms": 1800000
  }
  ```
- New endpoint `POST /api/simple/recording-sessions/{id}/split`:
  ```json
  {
    "split_points": [{"at_seconds": 1845.2, "name": "Budget review"}, {"at_seconds": 3210.0, "name": "Client call"}],
    "preserve_parent": true
  }
  ```
  Creates N child sessions, splits audio via ffmpeg (lossless cut at silence boundaries when possible), splits transcript by timestamp, parent marked `is_split_parent` and optionally hidden from list view.
- Audio splitting: prefer ffmpeg keyframe-aware cuts to avoid re-encoding. Falls back to re-encode if container needs it.
- Transcript splitting: walk the existing `transcript_diarized` segments by timestamp, group each segment into the child whose range contains its `start_time`.
- Idempotent: re-running with same split_points is a no-op.
- New endpoint `POST /api/simple/recording-sessions/{id}/unsplit` (children → parent): reverse operation. Concatenate audio, merge transcripts, delete children, parent goes back to normal status.

### Frontend

- New "Split into meetings" button on the SessionDetails page (next to existing Export buttons)
- On click: fetches proposed-splits, opens a `<SplitMeetingsModal>`:
  - Renders a horizontal timeline scrubber showing audio waveform + proposed split markers
  - Each marker shows time + transcript snippet from before/after
  - User can drag markers, add new ones (click on timeline), remove (click X)
  - Name input per child segment
  - "Apply" button → backend call, progress bar, redirect to first child session
  - Cancel button → no-op
- On the parent session's SessionDetails page (if it had been split):
  - Banner: "This session was split into N meetings: [list of links]. Reverse this split?"
  - "Reverse split" button calls the unsplit endpoint
- Sessions list view: by default hide `is_split_parent` rows (they're not "real" meetings any more). Setting toggle "Show split parents" for users who want to see the originals.

### Settings → Audio

- "Suggest splits after recording" toggle (default **ON**). When recording ends + transcription completes, the proposed-splits endpoint is called silently in the background. If splits are found, a passive notification: "We noticed 3 long silences in this recording. Want to split it into separate meetings?" with a "Review splits" link.
- "Auto-apply silent splits without asking" toggle (default **OFF**). For power users in the always-on capture-all-day workflow — they trust the algorithm and want zero clicks. Should never be the default.
- "Minimum silence to suggest a split" slider: `5 min / 10 min / 30 min / 1 hour / 2 hours` (default **30 min**).

## Effort estimate

- Backend: ~3 hours (alembic mig + 2 new endpoints + audio/transcript split + tests)
- Frontend: ~3 hours (modal + timeline scrubber UI + sessions-list filtering + settings)
- Total: 6-8 hours focused session

## Out of scope

- AI-driven topic-based split detection (use silence-only for v1; topic detection is v2)
- Real-time live-meeting boundary detection during always-on capture (deferred until silence-based post-process is validated in production)
- Cross-session merging (combining two meetings into one) — separate feature
- Splitting based on speaker change (e.g. "the meeting with Bob is over, now I'm meeting with Carol") — silence handles most of this in practice

## Risk

- ffmpeg audio splitting can re-encode → quality loss. Mitigation: prefer keyframe-aware cuts. Most browser-recorded audio uses Opus/webm which has reasonable keyframe intervals.
- Audio + transcript split timestamps must align exactly. Mitigation: use the canonical Parakeet timestamp output; never trust client-side timestamps for splits.
- Storage cost doubles temporarily until the parent is hard-deleted. Mitigation: schedule parent hard-delete 7 days after split, or behind a user "permanently delete original" button.

## When to schedule

Whenever Aaron decides — there's no urgency now that v3.23.2 disables the in-recording split. The current behavior ("one recording = one session, always") is safe and consistent. The post-process feature is pure additive value.

Suggested cadence: after the per-seat / org-invite work in v3.24, before the Brigade federation deploy. So probably v3.25 or v3.26.

## Related

- Memory `feedback_524_zombie_sessions_watchdog` — earlier work on stale session cleanup
- v3.22.5 `project_meeting_ops_v3_22_5_state` — stuck-state recovery (different bug, same neighborhood)
- v3.23.1 — first attempt at fixing this with a 30-min threshold; v3.23.2 finished the job
