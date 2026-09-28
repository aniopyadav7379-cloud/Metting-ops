# Screen Recording: Architecture Design

Status: Draft for approval. Speculative — Aaron flagged as "future major
feature." Implementation tickets at the end of this doc.
Owner: Meeting-Ops team.
Authoring date: 2026-05-20.

## 1. Purpose and Framing

Today UC-Meeting-Ops captures audio. The transcript and summary are the
artifacts; everything visual the participants saw — slides, dashboards,
whiteboards, code, screenshares — is gone the moment the meeting ends.
For a lot of meeting types that is fine. For a growing set it is not:
demo walkthroughs, slide-driven keynotes, whiteboard sessions, design
reviews, code pairing, and "wait, can you scroll back to that chart?"
moments all lose their primary artifact when only the audio survives.

This doc designs a **screen recording** surface that captures the visual
stream alongside the audio always-on pipeline that already exists. It is
not a replacement for audio. It is not a substitute for the audio source
picker. It is a separate optional capture path, with its own lifecycle
and its own storage, that produces a video artifact attached to the same
`recording_sessions` row the audio path produces.

Aaron's verbatim framing:
*"Always-on screen recording (separate from audio source) — big scope
idea — video capture (getDisplayMedia with video), storage (probably
IndexedDB chunks like audio + server upload of completed video),
playback UI in SessionDetails, retention policy, privacy mode compat.
Definitely a future major feature. Don't bundle with audio source
picker. Could power a 'Granola but with video' tier."*

Why a user would want this:

- **Visual reference** for what was on screen during the meeting. The
  transcript answers "what was said"; the video answers "what were they
  looking at when they said it."
- **Slide deck capture** for screenshare meetings (Zoom / Meet / Teams).
  Participants frequently can't get the deck after the meeting; with a
  screen capture they at least have the rendered slides in context.
- **Whiteboard sessions** in conference rooms. Pairs naturally with the
  room recording surface — the room mic captures the conversation, a
  fixed-camera or shared-screen capture grabs the whiteboard / projected
  content.
- **"Granola plus video" product differentiation**. Granola owns the
  audio-only summary niche. A video-attached transcript with synced
  playback is a differentiated tier that nobody in the AI-meeting-notes
  category does well today.
- **AI vision summaries** (Phase 3). Frame extraction at slide-change
  boundaries plus Qwen 3.6 35B-A3B-Vision on bigboy can OCR slides,
  caption diagrams, transcribe whiteboards, and feed visual content
  into the same summary pipeline the transcript flows through. The
  groundwork is already on the rack.

The design goals are:

1. **Audio path stays untouched.** AlwaysOnContext, vadEngine,
   audioSourceStream, the `/chunks` + `/chunks-text` endpoints, and the
   privacy-mode local-only path all keep working exactly as they do
   today. Video is an *additional* capture that piggybacks on the same
   session lifecycle.
2. **Reuse existing infrastructure.** `getDisplayMedia` is already
   wired in `audioSourceStream.ts` (commit `8731297`) for tab + system
   audio capture. We extend the same primitive to also pull the video
   tracks the picker returns instead of discarding them. The chunk
   POST pattern stays the upload contract. The recording_sessions row
   stays the artifact owner.
3. **Don't propose a different storage layer.** Garage (S3-compatible)
   is the object store. New bucket, same Garage cluster.
4. **Don't propose a new database.** Extend `recording_sessions` with
   nullable video columns + add one auxiliary table for resumable
   chunk tracking.
5. **Phase strictly.** Phase 1 ships Personal Screen + Audio on Chrome
   only. Conference room screen, multi-participant capture, and AI
   vision are explicit Phase 2 / 3 work.

## 2. What Already Exists (Survey Result)

Before designing anything new, a survey of the codebase on `magicunicorn`
shows the following directly relevant to the screen recording path:

### Frontend

- `frontend/src/utils/audioSourceStream.ts` (304 lines) — **already
  calls `getDisplayMedia` for tab and mic+tab audio modes.** It pulls
  `{ video: true, audio: true }` as a compat fallback when the browser
  refuses audio-only display media, then immediately stops the video
  track. **This is the biggest available shortcut: the video track is
  already in our hand at the moment we currently throw it away.**
  Extending this file to retain the video track when the user opts
  into screen recording is the smallest possible Phase 1 change.
- `frontend/src/utils/vadEngine.ts` (445 lines) — VAD-driven chunk
  emitter for audio. Has a pluggable `openStream` opener that already
  takes an `OpenedAudioSource` from `audioSourceStream.ts`. The video
  pipeline does NOT need to touch this; it runs in parallel on a
  separate MediaRecorder.
- `frontend/src/contexts/AlwaysOnContext.tsx` (1769 lines) — owns
  session start/stop, chunk upload queue, privacy-mode gate, local
  session store (IndexedDB), reconnect logic. **Adds a parallel
  `videoRecorderRef` and a parallel chunk queue** for video. Session
  lifecycle stays unified; the video pipeline is a sidecar.
- `frontend/src/components/AlwaysOnControl.tsx` (1069 lines) — start
  UI, audio source picker, privacy toggle. A new toggle / radio group
  ("Record screen too") goes here without disturbing the existing
  audio source picker.
- `frontend/src/components/DesktopBrowserRecorder.tsx` — the explicit
  Record button surface that already uses MediaRecorder with
  `getDisplayMedia({ video: true })` for screen capture. Reusable as
  a reference implementation.
- `frontend/src/services/localSessionStore.ts` — IndexedDB-backed
  storage for privacy-mode sessions. Video chunks slot in alongside
  audio chunks in a new `video_chunks` object store.
- `frontend/src/pages/SessionDetails.tsx` (3348 lines) — already
  renders an `<audio>` player. A `<video>` player goes in a new tab
  when `session.video_url` is present.

### Backend

- `backend/api/recording.py` (chunks endpoint at line 540) — the
  audio chunk POST that AlwaysOnContext targets. The video pipeline
  does NOT extend this endpoint; we add a parallel
  `/api/recordings/sessions/{id}/video-chunks` endpoint with its own
  resume + finalize lifecycle.
- `backend/database/models.py` (`RecordingSession` at line 31) —
  extends with `video_url`, `video_metadata`, `video_status`. No
  schema redesign.
- `backend/services/room_recorder.py` — server-side audio recorder
  for conference rooms. Out of scope: rooms cannot screen-record
  themselves (the server has no display). Phase 2 may add a
  satellite-side companion-app screen capture for room hosts.
- `RECORDINGS_DIR` (`/app/recordings`) — current local-disk
  persistence for audio. Video does NOT use this; video goes
  straight to Garage.

### What does NOT exist yet

- Any S3 / Garage client in the backend. `boto3==1.35.21` is already
  in `requirements-docker.txt` but no module uses it. **First Garage
  client lands as part of this work.**
- Any video-related column on `recording_sessions`.
- Any video chunk / video session table.
- Any retention sweeper for video. (Retention sweeper for audio
  ships in CR-008 for Conference Room Phase 2; the same scheduler
  picks up video.)

### Migrations

Latest migration through `024_satellite_device_secret`. Next migration
this design touches: `025_session_video`.

## 3. Three Distinct Use Cases

The video capture problem has three flavors. Phase 1 ships only the
first; Phase 2 and Phase 3 cover the others.

### 3.1 Personal screen + audio (Phase 1 only)

A single user is at a desktop browser running an always-on session.
They flip a "Record screen too" toggle. The browser prompts the OS
screen-share picker. The user picks a tab / window / entire screen.
Video frames and audio (mic, tab audio, or mic+tab depending on the
existing audio source picker) capture in parallel. On stop the video
artifact is attached to the same session row as the audio.

Use cases:

- Tab meetings (Zoom-in-tab, Meet, Teams web)
- Demos and walkthroughs the user wants to keep
- Solo screencasts ("record this for the team")
- AI training data (with explicit opt-in — explicit user gesture
  required by the OS prompt every time)

Hardware boundary: the user's laptop, the user's browser, the
user's consent (OS-level picker every session). No infrastructure
implications beyond Garage storage growth.

**Phase 1 ships this and only this.**

### 3.2 Conference room screen (Phase 2)

A physical conference room has a projector / shared screen showing
slides, dashboards, code, or a whiteboard. We want that content
attached to the room recording. Options:

- **Companion-app host capture.** A room-host machine running the
  Mac/PC companion app (`docs/companion-app-design.md`) runs the same
  capture pipeline as 3.1, scoped to the host's display, and POSTs
  the video to the room's active recording session. No new hardware
  per room — leverages whatever PC is already driving the projector.
- **HDMI capture appliance.** A dedicated device (BlackMagic /
  generic HDMI-to-USB) on the room's HDMI splitter, fed into a
  RoomHost Pi / mini-PC that POSTs video chunks the same way
  satellites POST audio. Higher fidelity, more hardware to manage,
  Phase 3+ territory.

Phase 2 ships the companion-app version. Phase 3 may add the HDMI
appliance for "appliance tier" deployments.

### 3.3 Multi-participant capture (Phase 3, speculative)

Each remote participant's screen feed is captured separately. This
requires either:

- A LiveKit / Daily / mesh WebRTC room infrastructure that publishes
  each participant's screen as a separate track. We don't have
  this and adding it is a major architectural piece (not in scope
  for this doc).
- Per-participant browser clients running the Phase 1 pipeline and
  uploading to the same `recording_sessions` row with a participant
  identifier. Easy to design, hard to coordinate operationally
  (every participant needs the extension / page open / permissions
  granted).

Either path is Phase 3 minimum. **Out of scope for this doc beyond
this paragraph.**

## 4. Capture Mechanics

The browser primitive is `navigator.mediaDevices.getDisplayMedia(...)`.

```ts
const stream = await navigator.mediaDevices.getDisplayMedia({
  video: {
    frameRate: { ideal: VIDEO_FRAMERATE_DEFAULT, max: VIDEO_FRAMERATE_MAX },
    width:  { ideal: VIDEO_TARGET_WIDTH },
    height: { ideal: VIDEO_TARGET_HEIGHT },
    displaySurface: 'browser',  // hint, not enforced
  },
  audio: false,  // audio is owned by audioSourceStream.ts, NOT this call
});
```

The picker dialog is browser-controlled. We don't pre-select the
surface — that's a user-gesture requirement enforced by the browser
and we lean into it as the consent boundary.

### 4.1 What we capture vs. what we discard

| Knob | Default | Range | Why this default |
|---|---|---|---|
| Frame rate | 15 fps | 5 / 15 / 30 / 60 | Slides + dashboards rarely change faster than 1 fps; 15 fps is comfortable for cursor motion and smooth enough for demos. 30 fps doubles the bitrate without doubling the value. 60 fps wastes everything for the meeting case. |
| Resolution | 1280x720 (downscaled if source bigger) | 854x480 / 1280x720 / 1920x1080 / native | 720p is "I can read the slide text" but ~3x cheaper to store than 1080p. 480p loses small slide text. 1080p+ is opt-in for demo-heavy users. |
| Encoding | VP9 (Chrome native, well-supported by MediaRecorder) | VP9 / VP8 / H.264 / AV1 (when supported) | VP9 is the only codec MediaRecorder ships across Chromium with strong compression. H.264 is opt-in for compatibility (Safari playback). AV1 is too new in MediaRecorder; revisit Phase 2. |
| Audio in video | none | none | Audio is captured separately by the audio pipeline. We do NOT bake audio into the video container — the transcript already owns timing alignment, and the audio pipeline is the canonical artifact. The video is a visual-only track. |
| Bitrate target | 1.5 Mbps | 0.5 / 1.5 / 3 / 5 Mbps | Slides at 720p VP9 fit in 1 Mbps comfortably; demos with motion need closer to 3 Mbps. Default targets the slide case. |

These knobs surface in Settings (one screen, not a per-session
picker). Phase 1 ships only the defaults plus an on/off toggle.

### 4.2 The stream-end signal

If the user closes the captured tab / window or clicks the "Stop
sharing" banner Chrome inserts at the top of the screen, the video
track ends. We register a `track.addEventListener('ended', ...)`
handler exactly like `audioSourceStream.ts` already does for tab
audio, and on end we **finalize the video portion of the session
gracefully** without stopping the audio capture. The session row
keeps `video_status='finalized_early'` and the audio pipeline
keeps running. This is the behavior Aaron asked for in spirit
("don't couple to the audio path"): video can die independently
without taking the meeting recording down with it.

### 4.3 Audio coordination (read carefully)

The video pipeline is **separate from the audio source mode**
(`mic | tab | mic+tab`). The user can:

- Record screen + mic (typical demo recording)
- Record screen + tab audio (typical meeting recording)
- Record screen + mic+tab (mixed)
- Record screen with NO audio capture (unusual but supported —
  privacy-conscious user, or pure visual reference)

This is enforced by **not asking for audio in the video
`getDisplayMedia` call**. Audio comes from the existing
`audioSourceStream.openAudioSourceStream(...)` path. The video
`getDisplayMedia` call always passes `audio: false`. **No
duplicate audio tracks. No double-counting. Audio path stays
the canonical source.**

There is ONE gotcha: when the user picks "screen + tab audio" and
in the SAME OS picker also picks "share tab audio", the audio is
captured in BOTH the audio pipeline AND would be in the video
pipeline if we asked for it. We don't ask, so no duplicate. But
we DO surface this in the UI as a one-line hint: *"Audio is
captured by the audio pipeline above. The screen recording
captures video only."*

## 5. Client-Side Flow

```
                                       getUserMedia / getDisplayMedia for audio
                                       (existing path, unchanged)
                                          |
                                          v
+--------------------------+         +----------------------+
|  audioSourceStream       | --audio>|  vadEngine (MicVAD)  |
|  (mic | tab | mic+tab)   |         |  -> VadAudioChunk    |
+--------------------------+         +----+------+----------+
                                          |      |
                                          |      v
                                          |   POST /api/recordings/sessions/
                                          |        {id}/chunks (or chunks-text)
                                          |
                                          v
                                       transcript + summary path
                                       (this all stays the same)

                          === NEW: video pipeline (parallel) ===

+-----------------------+           +----------------------+
| getDisplayMedia       | --video>  |  MediaRecorder       |
| ({video:true,         |           |  + requestData() every|
|   audio:false})       |           |  CHUNK_INTERVAL_MS    |
+-----------------------+           +----+------+----------+
                                         |      |
                                         |      v
                                         |   IndexedDB rolling
                                         |   buffer (privacy mode
                                         |   or upload retry)
                                         |
                                         v
                                      POST /api/recordings/sessions/
                                          {id}/video-chunks
                                          (multipart, indexed, resumable)
                                          v
                                      finalize-video -> Garage assemble
                                          -> optional ffmpeg transcode
                                          -> recording_sessions.video_url
```

### 5.1 MediaRecorder chunking pattern

MediaRecorder is one of the messier browser APIs. Default behavior:
you start it, hand it a stream, call `.stop()`, and it gives you ONE
blob with the entire recording in a single chunk. That doesn't
work for us — long meetings would mean huge in-memory blobs and a
single-shot upload that breaks on every disconnect.

The right pattern:

1. `recorder.start(CHUNK_INTERVAL_MS)` — pass a timeslice
   argument. MediaRecorder fires `dataavailable` periodically,
   yielding one Blob per slice. **Phase 1 defaults to 30000 ms
   (30 s slices).**
2. Each `dataavailable` Blob is added to an in-memory queue keyed
   by an incrementing `chunk_index`.
3. The queue drains to two targets in parallel:
   - **IndexedDB** (`video_chunks` object store), keyed by
     `(sessionId, chunkIndex)`. Acts as a rolling local buffer
     for privacy mode AND as upload retry storage when the
     network drops.
   - **Server upload** via `POST /api/recordings/sessions/{id}/
     video-chunks` with the chunk blob + `chunk_index` field.
     Server stores each chunk in Garage at
     `meeting-video/{org_id}/{session_id}/chunks/{chunk_index}.webm`.
4. On successful 200, the IndexedDB entry's `uploaded` flag
   flips. A periodic re-sync sweep retries any
   `uploaded=false` chunks until they land.
5. On `MediaRecorder.stop()` (user clicks Stop, session ends,
   video track ends), the final partial blob also flushes, and
   the client POSTs `/finalize-video` so the server can assemble
   the chunks and kick off transcode.

**Caveat: VP9 chunked-WebM concatenation.** WebM chunks emitted by
MediaRecorder DO concatenate cleanly when each slice is a complete
cluster boundary, which MediaRecorder targets when you pass a
timeslice. Browser-side concatenation by `Blob` concat works for
playback in Chrome. Server-side concatenation needs ffmpeg
(`concat demuxer` for VP9 / WebM) — see Section 6. We never rely
on "just append the bytes"; the server always reassembles via
ffmpeg.

### 5.2 IndexedDB strategy

The existing `localSessionStore.ts` opens a single IndexedDB
database with the audio chunk store. We add a sibling object
store:

```js
db.createObjectStore('video_chunks', {
  keyPath: ['sessionId', 'chunkIndex'],
});
```

Storage budget:

- Chrome's default quota is ~60% of free disk per origin.
- A 1-hour meeting at 15 fps / 720p / 1.5 Mbps ~= 675 MB. Comfortable
  for any modern laptop.
- A 4-hour all-hands ~= 2.7 GB. Still fine on most disks.
- Auto-eviction is opt-in; we call
  `navigator.storage.persist()` at session start to ask the
  browser not to evict mid-meeting.

If the browser is going to OOM IndexedDB (catastrophic disk
pressure), the chunk write throws a quota-exceeded error. We
catch, fire a toast (*"Local storage is full — video upload-only
mode engaged"*), and continue uploading to the server without
the local mirror. Privacy mode users in this state get a hard
stop because privacy mode forbids server upload — see Section 7.

### 5.3 Memory pressure

MediaRecorder buffers internally. Long sessions can grow
significantly because the encoder keeps reference frames in
memory for compression. Mitigations:

- Slice on a regular timeslice so the encoder flushes.
- For very long sessions (Phase 2), an opt-in **slice rotation**:
  every N chunks (default 100, ~50 min at 30 s slices) we
  `recorder.stop()` and immediately `recorder.start()` a new
  recorder on the same stream. Discontinuity in the encoded
  bitstream is invisible at playback time because the server
  reassembles via ffmpeg with `concat demuxer`. **Phase 2.**
- Chrome's WebCodecs API + OffscreenCanvas pipeline can move
  encoding off the main thread. **Phase 2.**

### 5.4 Tab close / browser crash recovery

- **Graceful tab close**: a `beforeunload` handler calls
  `recorder.requestData()` to flush the current slice into
  IndexedDB before the tab dies. On next session open, a
  pending-uploads sweep finds any `uploaded=false` chunks for
  the same `sessionId` and re-uploads them.
- **Browser crash**: same recovery — the chunks already in
  IndexedDB stay there; the next browser open finds and uploads
  them. The server's `/finalize-video` endpoint is idempotent
  and can be called on the next session open after the chunks
  catch up.
- **Power loss mid-write**: the latest chunk may be corrupt; we
  ship it anyway and the server skips unreadable chunks during
  ffmpeg concat. Loss is bounded to one 30 s slice.

## 6. Server-Side Storage

### 6.1 Garage

Garage already exists on bigboy. We add one new bucket:

- `meeting-video` (single bucket; per-org and per-session
  prefixes inside it). Object lifecycle policies set on the
  bucket govern retention (Section 8).

Path scheme:

```
meeting-video/
  org-{org_id}/
    session-{session_uuid}/
      chunks/
        000001.webm
        000002.webm
        ...
      assembled.webm           # ffmpeg concat output, when ready
      assembled.mp4            # optional H.264 transcode for Safari/iOS
      poster.jpg               # extracted thumbnail
      metadata.json            # codec / resolution / bitrate / duration
```

Garage's flat namespace doesn't need pre-creation of
"directories." We just write objects at these paths.

### 6.2 Upload flow

1. **Chunk POST** lands at
   `/api/recordings/sessions/{id}/video-chunks`. Body is
   multipart with the chunk Blob + JSON metadata
   (`chunk_index`, `total_chunks_expected_so_far`,
   `duration_ms`, `bytes`, `client_sha256`). Server writes the
   blob to Garage at `chunks/{NNNNNN}.webm` and inserts a row
   in `video_chunks` for resumability.
2. **Finalize POST** at
   `/api/recordings/sessions/{id}/finalize-video` enqueues a
   background job that:
   - Lists all chunks for the session from Garage.
   - Validates chunk indexes are contiguous (gaps logged but
     non-fatal — ffmpeg can concat with gaps).
   - Streams the chunks through ffmpeg's `concat demuxer`
     into `assembled.webm`.
   - **Optionally** transcodes to H.264 / MP4 for Safari +
     iOS compatibility. Off by default, on per-org switch.
     `assembled.mp4` is the output.
   - Extracts a thumbnail at the median timestamp into
     `poster.jpg` (`ffmpeg -ss <median> -frames:v 1`).
   - Writes `metadata.json` with codec + resolution + bitrate
     + duration extracted via `ffprobe`.
   - Updates `recording_sessions.video_url` (signed Garage
     URL pattern via `/api/recordings/sessions/{id}/video`
     redirect) and `video_status='ready'`.
3. The background job runs in the existing FastAPI background
   task pool. No new queue infra; the failure mode is "video
   stays in `processing` until manual re-enqueue" and that's
   fine for Phase 1.

### 6.3 ffmpeg

Already in the backend image (whisper / audio pipelines depend
on it). VP9 / WebM concat works out of the box. H.264 transcode
uses `libx264` (CPU) or `h264_nvenc` if the container has GPU
passthrough. The room sessions run on bigboy where GPU is
available; CPU transcode is the safe Phase 1 default and a GPU
upgrade is a Phase 2 config flip.

### 6.4 Chunk-format choice (defends Phase 1 simplicity)

We considered three alternatives:

| Format | Pros | Cons | Verdict |
|---|---|---|---|
| WebM with VP9 (Phase 1) | MediaRecorder default in Chrome; smallest moving parts; ffmpeg native | Not Safari-native (Safari 17 has WebM but limited); transcode optional | **Pick.** Phase 1 ships this. |
| Fragmented MP4 (fMP4) | Safari-native; HLS-friendly | MediaRecorder support is uneven; needs polyfill (`mp4-muxer`); Phase 1 risk | Defer to Phase 2. |
| Raw frame chunks (WebCodecs + OffscreenCanvas) | Smallest possible chunks; per-frame timestamps | Complex; codec-level handling; runs us into MOOV-box reassembly | Phase 3 if at all. |

### 6.5 Storage cost estimation

Rough numbers, defending the Phase 1 default (1.5 Mbps / 720p /
15 fps):

| Scenario | Per-meeting | Per-user / month | Per-100-users / year |
|---|---|---|---|
| 30 min, slides-heavy | ~340 MB | ~7 GB (5 meetings/week) | ~8 TB |
| 60 min, mixed slides + demo | ~675 MB | ~14 GB | ~17 TB |
| 60 min, native res 1080p / 3 Mbps | ~1.35 GB | ~28 GB | ~34 TB |

bigboy's Garage allocation today is in the low TB. Phase 1
needs a deliberate quota decision — see Section 16. Aaron's
"50 TB/year for 100 active video users" is not a free decision.

## 7. Privacy Mode Compatibility

Privacy mode (`localOnly`) is a hard contract: nothing leaves
the device. The video pipeline honors this:

- Video chunks land in IndexedDB only. The server upload step
  is **skipped entirely** when `activeLocalOnly === true` at
  session start.
- Playback works from the local IndexedDB cache. SessionDetails
  detects `is_local && has_local_video` and renders the
  `<video>` element from a `URL.createObjectURL(blob)` source
  assembled from concatenated chunks.
- **No transcode in local-only mode.** The browser plays the
  raw chunked WebM directly via Media Source Extensions, or
  via a single concatenated blob (`new Blob(chunks)`). VP9 is
  Chrome-native so playback is trivial.
- The "Export video" button (Section 7.1) creates a single
  concatenated Blob download for the user — that's the user
  manually choosing to export, which is consistent with the
  privacy contract (the user is in control).

### 7.1 Local-only export

A new button in SessionDetails: *Export video* -> downloads
`assembled.webm` from a `Blob` concat in the browser. The user
can then re-upload manually elsewhere if they choose, but the
default privacy posture is preserved.

### 7.2 Trade-off

IndexedDB storage budget is bounded. A user with privacy mode +
heavy video usage will saturate their browser quota faster than
the audio path does (audio is ~50x smaller per minute than 720p
video). We surface this in the privacy-mode docs and add a
*"Estimated free recording time: ~3 h 20 m"* indicator in the
control panel based on `navigator.storage.estimate()`.

If the user hits the quota mid-meeting, video capture stops
(with a toast) and the audio capture continues. The session
row stays `video_status='quota_exceeded_local'`.

## 8. Retention & Size Management

Video is significantly more expensive than audio. Retention has
to be a first-class concern from Day 1.

### 8.1 Defaults

| Resource | Default retention | Configurable |
|---|---|---|
| Audio + transcript | inherits room policy / 90 days (rooms) / unset for personal | yes (per-org, per-room) |
| **Video** | **30 days** | yes (per-org, per-room, per-session "pin") |

Phase 1 only supports per-org. Phase 2 adds per-room and per-session
pin.

### 8.2 Enforcement

- Garage lifecycle policy on the `meeting-video` bucket with prefix
  rules per-org. Simplest sweeper.
- Belt-and-suspenders: a nightly Python job in
  `services/retention_sweeper.py` (already designed in CR-008 for
  audio) that hard-deletes via the Garage API and clears
  `recording_sessions.video_url`. Idempotent.
- "Pin" support (Phase 2): a `recording_sessions.video_pinned`
  boolean. Sweeper skips pinned sessions. UI shows a lock icon on
  pinned sessions. Per-org quota for pin count to prevent abuse.

### 8.3 Per-org quota

`organizations.video_storage_quota_mb` (new column, nullable, NULL
== unlimited / no enforcement). When set:

- Server tracks total video bytes per-org in
  `org_video_storage_usage` (small derived counter table or
  computed on demand).
- When a video chunk would push the org over quota, the upload
  is rejected with `413 Payload Too Large` and the client falls
  back to local-only mode for the remainder of the session
  (toast: *"Video storage quota exceeded — recording continues
  locally only"*).
- A daily job recomputes the counter to correct drift.

Phase 1 ships the column + simple enforcement. Phase 2 ships
the UI to manage quota + visualization of usage.

## 9. Playback UI

SessionDetails grows a new tab: **Video** (visible iff
`session.video_url` is set OR session is local-only with video
chunks in IndexedDB).

### 9.1 Component layout

```
+---------------------------------------------------------------+
| Session: Q3 Strategy Review                                  x|
+--------+-------+----------+----------+---------+--------------+
| Audio  | Video | Transcript | Summary | Action Items | Files  |
+--------+---*---+----------+----------+---------+--------------+
              |
              v
+---------------------------------------------------------------+
|   [video player, 16:9, controls]                              |
|                                                                |
|   00:00 -----o----------------------- 47:23                   |
|                                                                |
|   Chapters from VAD speech boundaries:                         |
|   > 00:00 Slide intro                                          |
|   > 04:12 Q1 numbers                                           |
|   > 12:30 Q2 outlook                                           |
|   > 22:00 Action items                                         |
+---------------------------------------------------------------+
```

### 9.2 Sync with transcript + summary

- **Transcript click -> seek video.** Clicking a transcript line
  with timestamp `00:14:23` seeks the video to that timestamp.
  Already plumbed for audio via the existing `<audio>` element;
  video reuses the same `currentTime` setter pattern.
- **Summary slice click -> seek video.** Same behavior, scoped
  to the slice's `transcript_start` -> video timestamp
  conversion.
- **Bidirectional**: video playback fires `timeupdate` events,
  which highlight the corresponding transcript line and
  summary slice (already implemented for audio; video reuses
  it).

### 9.3 Chapters

Auto-generated from VAD speech boundaries (`elapsed_seconds`
values already on transcript segments). Chapter title comes
from either:

- The first 6-10 words of the corresponding transcript segment
  (Phase 1 default — cheap and good enough).
- An LLM-generated chapter title from the slice summarization
  pipeline that already exists (Phase 2 upgrade).

### 9.4 Download

Buttons:

- *Download WebM* — direct Garage signed URL (or local Blob for
  privacy-mode sessions).
- *Download MP4* — only visible when the H.264 transcode ran
  (per-org switch).
- *Download poster.jpg* — small button next to the player.

### 9.5 Phase 1 vs later

Phase 1 ships:

- Video tab, native `<video>` player, transcript-sync seek,
  chapters from VAD, download WebM, download MP4 (when present).

Phase 2 ships:

- LLM-generated chapter titles, slice-level scrubbing, picture-in-
  picture, theater mode.

Phase 3 ships:

- AI vision overlays (slide OCR floating over the timeline, slide
  thumbnail strip).

## 10. AI Vision Integration (Future / Phase 3)

Frame-level processing is where the "Granola but with video"
positioning gets real. We already have a vision-capable model on
midboy1 (`Qwen3-VL-8B` for art QA, 8087/tailscale <gpu-node>)
and Qwen 3.6 35B-A3B with vision capability is locally hostable
on bigboy. Phase 3 unlocks:

### 10.1 Slide OCR

At slide-change boundaries (detected via perceptual hash diff
between consecutive sampled frames), extract the frame, run OCR
(via the vision model with an OCR prompt OR a dedicated lightweight
OCR like PaddleOCR), and emit the recognized text as a synthetic
transcript stream tagged `provenance='slide_ocr'`. The
transcript merge step distinguishes spoken vs. slide-rendered
text by provenance tag.

### 10.2 Slide summarization

At each detected slide change, send the frame + the previous
~30 s of transcript to Qwen 3.6 vision: "Summarize what's on
this slide and how it relates to what was just said." Output is
attached to the slice that contains that timestamp. The summary
pane gets a "slides shown" sub-section.

### 10.3 Whiteboard transcription

Same primitive as slide OCR but with a different prompt: "Transcribe
the handwriting and ASCII-art-ify any diagrams." Useful for design
review and brainstorming.

### 10.4 Frame sampling cadence

- Default: 1 frame per second (sampled — not encoded — via a
  hidden `<canvas>` that draws the video frame at 1 Hz). Cheap.
- Slide-change detector: perceptual hash diff threshold; only
  the changed frames go to the vision model. Most slides hold
  for 30+ seconds, so the vision model load is typically 1
  call per minute of meeting.
- Bigboy GPU has plenty of headroom (3090 + RTX 6000) for this
  cadence even at scale.

### 10.5 Phase 3 prerequisites

- Phase 1 + 2 video storage + playback shipped.
- A `frame_extractor` service that pulls sampled frames from
  Garage video into a working directory, runs hash diff, sends
  changed frames to the vision model.
- New `recording_session_frames` table for sampled frames +
  OCR text + vision-summary attachment.
- UI: slide thumbnail strip under the video, click to seek.

**Not in scope for Phase 1 tickets.** Listed here to motivate
the design choices that keep Phase 3 cheap to add later (e.g.
storing the original video in Garage so frame extraction is
easy; not committing to a fixed transcode).

## 11. Performance Considerations

The video pipeline competes with the audio pipeline for CPU,
memory, and network. Phase 1 accepts known costs; Phase 2 has
optimization headroom.

### 11.1 Phase 1 known costs

- **MediaRecorder runs on the main thread.** Encoding VP9 at
  15 fps / 720p takes ~5-15% of one CPU core on a modern laptop.
  Visible in DevTools, not visible in UX for most users. Slow
  laptops may see frame drops in the rest of the page UI.
- **VAD pipeline runs in parallel.** Separate from the video
  pipeline at the API level, but on the same CPU. Combined load
  is ~20-30% on slower laptops.
- **IndexedDB writes** every chunk are async but serialize
  through a single transaction queue. Not a bottleneck at 30 s
  slice intervals.
- **Network**: ~1.5 Mbps sustained upload. Comparable to a
  video call. Should not surprise the user, but we'll add a
  data-usage indicator (Phase 2).

### 11.2 Phase 2 optimization paths

- **WebCodecs + OffscreenCanvas** in a Web Worker. Encoding off
  the main thread eliminates the page-UI jank entirely. Requires
  re-implementing the MediaRecorder behavior on top of
  `VideoEncoder` from WebCodecs (Chrome 94+, well-supported now).
- **Frame pre-downscale** before encoding. If the source is
  larger than the target, downscale via `OffscreenCanvas
  drawImage` before feeding to the encoder. Saves encoder
  cycles.
- **Variable frame rate** — drop duplicate frames when nothing
  changes on screen. Halves the bitrate for slide meetings.

Phase 1 doesn't need this. Phase 2 can add it surgically.

## 12. Browser Support

| Browser | getDisplayMedia (video) | MediaRecorder (VP9) | Support level |
|---|---|---|---|
| Chrome 120+ (desktop) | Full | Full | **Phase 1 target** |
| Edge 120+ (desktop) | Full | Full | **Phase 1 target** (Chromium-based) |
| Firefox 122+ | Full (video); audio partial | VP8 native, VP9 in newer versions | Phase 2 |
| Safari 17+ (macOS) | Video only; no audio | MP4 native, no VP9 | Phase 2 with format negotiation |
| Mobile Safari (iOS) | Not supported | n/a | **Never** — iOS WebKit blocks getDisplayMedia. |
| Chrome Android | Not supported | n/a | **Never** — Android blocks getDisplayMedia in browser. |

### 12.1 Detection + graceful degradation

UI detects support via the existing `isTabCaptureSupported()` and
a new sibling `isScreenVideoCaptureSupported()`:

```ts
export function isScreenVideoCaptureSupported(): boolean {
  return (
    typeof navigator !== 'undefined'
    && !!navigator.mediaDevices
    && typeof navigator.mediaDevices.getDisplayMedia === 'function'
    && /Chrome|Edg/.test(navigator.userAgent)  // Phase 1 narrowing
    && !/Mobile/.test(navigator.userAgent)
  );
}
```

When unsupported, the "Record screen too" toggle is hidden (not
just disabled) with no error noise. The audio pipeline keeps
working exactly as it does today.

## 13. Phased Rollout

### Phase 1 — Personal Screen + Audio MVP (target: 3-4 weeks)

Goal: a Chrome user can toggle "Record screen too" before
starting always-on, get an OS picker, capture screen + audio,
upload to Garage, play back in SessionDetails with sync to
transcript.

- Migration `025_session_video` (columns + `video_chunks` table).
- Backend Garage client (`backend/services/garage_client.py`,
  thin wrapper over `boto3`).
- Endpoints: `/video-chunks`, `/finalize-video`, `/video`,
  `DELETE /video`.
- Backend service:
  `services/video_finalizer.py` (background job: list chunks,
  ffmpeg concat, optional H.264 transcode, poster extract).
- Frontend:
  - `frontend/src/services/screenCapture.ts` — opens
    getDisplayMedia for video only, returns the stream.
  - `frontend/src/services/videoRecorder.ts` — MediaRecorder
    wrapper + chunk queue + IndexedDB writer + upload.
  - Toggle in `AlwaysOnControl.tsx`.
  - New `videoRecorderRef` parallel pipeline in
    `AlwaysOnContext.tsx`.
  - Video tab in `SessionDetails.tsx` with native `<video>`
    + transcript sync.
- Chrome only. Defaults 720p / 15 fps / 1.5 Mbps. No H.264
  transcode unless org switch is on. No per-room or
  per-session pin.
- Retention: 30 days, org-wide, Garage lifecycle policy.

What's deliberately not in Phase 1:
- Firefox / Safari support
- WebCodecs / off-main-thread encoding
- AI vision (frames, OCR, slide summaries)
- Conference room screen capture (Phase 2)
- Multi-participant capture (Phase 3)
- Per-session pin
- Slice rotation for very long sessions

### Phase 2 — Hardening + Firefox + Retention UX (target: 3-4 weeks)

- WebCodecs + OffscreenCanvas Worker pipeline (off main thread)
- Firefox support (negotiate codec; VP8 fallback if VP9 missing)
- H.264 transcode default on for orgs that ask for Safari
  compatibility
- Per-room retention policy
- Per-session pin
- Slice rotation for >2-hour sessions
- Storage quota UI for orgs + usage visualization
- Companion-app screen capture for room hosts (Conference Room
  Phase 2 integration)
- Better chapter generation (LLM-titled)
- Picture-in-picture player

### Phase 3 — AI Vision + Multi-Participant (target: TBD)

- Frame extractor service
- Slide OCR via vision model
- Slide summarization via vision model
- Whiteboard transcription
- Slide thumbnail strip in playback UI
- Multi-participant capture (per-browser uploads to same session
  OR LiveKit-based room track recording)
- HDMI capture appliance integration for conference rooms

## 14. Data Model Additions

Migration: `025_session_video`.

### 14.1 Extend `recording_sessions`

```sql
ALTER TABLE recording_sessions
    ADD COLUMN IF NOT EXISTS video_url TEXT,
        -- nullable; populated after finalize. Format:
        -- `/api/recordings/sessions/{id}/video` (server signs Garage URL on request)
    ADD COLUMN IF NOT EXISTS video_status VARCHAR(32),
        -- nullable when no video; otherwise one of:
        -- 'recording' | 'uploading' | 'finalizing' | 'ready'
        -- | 'failed' | 'finalized_early' | 'quota_exceeded_local'
    ADD COLUMN IF NOT EXISTS video_metadata JSONB,
        -- {"codec": "vp9", "width": 1280, "height": 720, "duration_s": 2842.3,
        --  "bitrate_bps": 1500000, "framerate": 15, "bytes": 532000000,
        --  "transcoded_mp4": true|false, "poster_path": "...jpg"}
    ADD COLUMN IF NOT EXISTS video_pinned BOOLEAN NOT NULL DEFAULT FALSE,
        -- Phase 2: skips retention sweep
    ADD COLUMN IF NOT EXISTS video_retention_expires_at TIMESTAMPTZ;
        -- nullable; null = inherit org policy
CREATE INDEX ix_recording_sessions_video_retention
    ON recording_sessions (video_retention_expires_at)
    WHERE video_retention_expires_at IS NOT NULL AND video_pinned = FALSE;
CREATE INDEX ix_recording_sessions_video_status
    ON recording_sessions (video_status)
    WHERE video_status IS NOT NULL AND video_status != 'ready';
```

### 14.2 New table: `video_chunks`

Tracks per-chunk upload state for resumability. Server-side
authoritative; client mirrors in IndexedDB for its own
retry logic.

```sql
CREATE TABLE video_chunks (
    id              SERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES recording_sessions(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    bytes           BIGINT NOT NULL,
    duration_ms     INTEGER,
    client_sha256   VARCHAR(64),
    garage_key      TEXT NOT NULL,         -- full Garage path inc. prefix
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, chunk_index)
);
CREATE INDEX ix_video_chunks_session ON video_chunks (session_id, chunk_index);
```

### 14.3 Extend `organizations`

```sql
ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS video_storage_quota_mb BIGINT,
        -- null = unlimited / no enforcement
    ADD COLUMN IF NOT EXISTS video_retention_days INTEGER NOT NULL DEFAULT 30,
        -- per-org override of the 30-day default
    ADD COLUMN IF NOT EXISTS video_transcode_mp4 BOOLEAN NOT NULL DEFAULT FALSE;
        -- whether finalize step also produces an H.264/MP4
```

### 14.4 Why no new top-level `videos` table

We considered modeling video as a sibling of `recording_sessions`
(a `videos` table with FK to sessions). Rejected: the video is
1:1 with the session conceptually, the existing `audio_file`
column is on `recording_sessions` directly, and the UI surfaces
video as a tab within session details rather than a separate
artifact list. Keeping video on `recording_sessions` minimizes
join overhead and matches the audio modeling decision.

## 15. API Surface

All new endpoints under `/api/recordings/sessions/{session_id}/`.

| Method | Endpoint | Purpose | Auth |
|---|---|---|---|
| `POST` | `/video-chunks` | Upload one chunk. Body: multipart with `chunk` (blob) + `chunk_index` (form) + optional metadata. Idempotent on `(session_id, chunk_index)`. | session owner OR internal-service token (Phase 2 companion app) |
| `GET` | `/video-chunks` | List uploaded chunk indexes for resume support. Client uses this on reconnect to skip already-uploaded chunks. | session owner |
| `DELETE` | `/video-chunks/{chunk_index}` | Delete a specific chunk (rare; for client-side error recovery). | session owner |
| `POST` | `/finalize-video` | Enqueue assembly + transcode. Returns immediately; status pollable via GET. | session owner |
| `GET` | `/video` | 302 redirect to a Garage signed URL (or stream proxy for orgs that don't expose Garage publicly). 5-minute signed-URL TTL. | session viewer |
| `GET` | `/video/status` | Polling endpoint for finalize progress: `{status, progress_pct}`. | session viewer |
| `DELETE` | `/video` | User-initiated delete of the entire video artifact (chunks + assembled + metadata). Audio + transcript untouched. | session owner OR org admin |
| `GET` | `/video/poster` | Returns poster.jpg or redirect to signed Garage URL. | session viewer |

Notes:

- The audio chunk endpoints (`/chunks`, `/chunks-text`) are
  **untouched.** Aaron's constraint.
- The internal-service token pattern from `room_recorder.py`
  (commit `7ea025f`) is reused for the Phase 2 companion-app
  upload path. Phase 1 user sessions auth via the normal JWT.

## 16. Open Questions for Aaron

These are decisions only Aaron can make. None of them block writing
the design doc, but all of them must resolve before Phase 1 ships.

1. **Storage budget.** What's the Garage allocation ceiling for
   video in Phase 1? At 100 active users producing ~14 GB / month
   each, the math is ~17 TB / year. Is that fine, or do we need a
   tighter retention default (14 days instead of 30) and/or a more
   aggressive per-user cap?

2. **Brand positioning.** Does the "Granola but with video"
   framing fit the UC story, or does it dilute the "build your
   own AI stack" positioning? If we ship video, do we lead with
   it in marketing or quietly ship it as a power-user feature
   first?

3. **AI vision day 1 or phased?** The vision model on bigboy is
   already there. Phase 1 could include a stripped-down OCR pass
   on slide changes, OR Phase 3 ships the entire vision story
   together. The trade-off is a meaningful demo on day 1 vs. a
   cleaner Phase 1 with less risk.

4. **Firefox + Safari Phase 1 or never?** Chrome-only Phase 1 is
   the safest path. Adding Firefox + Safari support is mostly
   codec negotiation and Phase 2 work. But if we expect Safari
   users in the target audience, we may want to commit to
   "ship Phase 1 with H.264/MP4 transcode default-on" so Safari
   playback works out of the gate — that doubles storage cost.

5. **Pricing tier.** Is video a premium-tier feature ("UC Plus")
   or default-included? If premium, we need entitlement plumbing
   in the toggle UI (read org subscription / Keycloak claim).
   If default, no entitlement work needed but Phase 1 has to
   handle a lot more usage than a feature-gated rollout.

## 17. Out of Scope (Explicit)

The following are **not** in this design:

- Real-time video broadcasting / WebRTC mesh (we're recording,
  not live-streaming).
- Per-frame editing / cuts / non-destructive video editing
  (Majiks-Screen handles that for a different product context).
- Multi-camera composition / picture-in-picture composing of
  multiple sources into one video.
- Live-streaming the captured video to external viewers during
  the meeting.
- Webcam capture (`getUserMedia({ video: true })` for the user's
  face). If a user wants that they share their webcam in their
  conferencing tool and we capture it as part of the screen share.
- HLS / DASH adaptive streaming. Direct Garage signed URLs with
  `<video src>` is enough for Phase 1 + 2. Phase 3 may revisit
  if multi-bitrate is needed.
- Server-side recording of remote participants (LiveKit-style).
  This is a major architectural piece; see Section 3.3.
- Closed-caption burn-in onto the video. Captions stay on the
  transcript layer and render as a synced overlay in the
  player.
- Watermarking the video. Phase 3 if anyone asks.

## 18. Compatibility Notes

- The new pipeline adds tables + columns. **No existing data is
  modified.** All current `recording_sessions` rows keep
  `video_url = NULL` and the Video tab does not render.
- The existing `/chunks` + `/chunks-text` endpoints are untouched.
  Audio behavior is byte-for-byte unchanged.
- The new `getDisplayMedia({ video: true })` call is independent
  of `audioSourceStream.openTabAudioOnly()`. The two can run
  concurrently because each gets its own user gesture / picker
  prompt. (The UI puts them in clear order: audio picker first,
  then a single "Record screen too" check that fires the second
  prompt when starting.)
- `RECORDINGS_DIR` (local disk audio path) is not used by video.
  Video lives entirely in Garage. The local disk doesn't grow
  because of this feature.

## 19. Ticket Breakdown

Each ticket is sized S (~1 day), M (~2-4 days), L (1-2 weeks).
All file paths are absolute under
`/srv/meeting-ops/src/`.

### SR-001 [M] Migration `025_session_video`
- **Scope**: add columns to `recording_sessions` + `organizations`;
  create `video_chunks` table; indexes per Section 14.
- **Deps**: none.
- **Files**: `backend/alembic/versions/025_session_video.py`,
  `backend/database/models.py` (extend `RecordingSession` +
  `Organization`), new `VideoChunk` model class.

### SR-002 [M] Backend Garage client wrapper
- **Scope**: `backend/services/garage_client.py` — thin wrapper
  over `boto3` with endpoint URL + access key from env vars
  (`GARAGE_ENDPOINT_URL`, `GARAGE_ACCESS_KEY`,
  `GARAGE_SECRET_KEY`, `GARAGE_BUCKET_VIDEO`). Provides
  `upload_chunk()`, `download_object()`, `signed_url()`,
  `delete_prefix()`, `list_prefix()`.
- **Deps**: SR-001.
- **Files**: `backend/services/garage_client.py` (new),
  `backend/services/__init__.py`, `backend/.env.example`
  (document new env vars).

### SR-003 [L] Backend video-chunk + finalize endpoints
- **Scope**: implement `POST /video-chunks`, `GET /video-chunks`,
  `DELETE /video-chunks/{n}`, `POST /finalize-video`,
  `GET /video`, `GET /video/status`, `DELETE /video`,
  `GET /video/poster`. Auth via existing
  `get_internal_or_user` (so the Phase 2 companion-app path
  comes free). Per-org quota enforcement on `POST /video-chunks`.
- **Deps**: SR-001, SR-002.
- **Files**: `backend/api/recording_video.py` (new — separate
  module from `recording.py` to keep the audio path clean),
  `backend/main.py` (router registration), Pydantic models.

### SR-004 [L] Backend video finalizer service
- **Scope**: `services/video_finalizer.py` — background job
  triggered by `/finalize-video`. Lists Garage chunks, runs
  `ffmpeg -f concat -i ... -c copy assembled.webm`, optionally
  transcodes to MP4 (`-c:v libx264 -preset medium -crf 23`),
  extracts poster via `ffmpeg -ss <median> -frames:v 1`,
  writes metadata.json + ffprobe output, updates session row.
  Idempotent on retry.
- **Deps**: SR-002, SR-003.
- **Files**: `backend/services/video_finalizer.py` (new),
  `backend/services/ffmpeg_wrappers.py` (new helper for the
  ffmpeg invocations + ffprobe parsing).

### SR-005 [M] Frontend screen-capture stream opener
- **Scope**: `frontend/src/services/screenCapture.ts` —
  `openScreenCaptureStream({ frameRate, width, height })`
  returning `{ stream, cleanup, onUnexpectedEnd }`. Calls
  `getDisplayMedia({ video: {...}, audio: false })`. Handles
  track-ended (user stops sharing). Detection helpers
  (`isScreenVideoCaptureSupported()`).
- **Deps**: none (independent of backend).
- **Files**: `frontend/src/services/screenCapture.ts` (new),
  `frontend/src/utils/audioSourceStream.ts` (no changes —
  deliberately).

### SR-006 [L] Frontend MediaRecorder + IndexedDB chunk pipeline
- **Scope**: `frontend/src/services/videoRecorder.ts` —
  MediaRecorder wrapper that emits chunks every
  `CHUNK_INTERVAL_MS` (30 s default). Chunk queue: writes to
  IndexedDB AND queues for server upload. Retry on
  upload failure with exponential backoff. `beforeunload`
  flush. Resume from IndexedDB on next session open.
  Quota detection + graceful degradation.
- **Deps**: SR-005, SR-003.
- **Files**: `frontend/src/services/videoRecorder.ts` (new),
  `frontend/src/services/localSessionStore.ts` (extend
  with `video_chunks` object store).

### SR-007 [M] Frontend always-on integration (toggle + wiring)
- **Scope**: Add "Record screen too" toggle to
  `AlwaysOnControl.tsx`. Plumb through `AlwaysOnContext.tsx`
  as a parallel `videoRecorderRef`. Start / stop / discard
  / pause wire to both audio and video. Privacy mode honored
  (no upload when local-only). Cross-surface guard updated to
  block the explicit Record button when screen recording is
  active.
- **Deps**: SR-005, SR-006.
- **Files**: `frontend/src/contexts/AlwaysOnContext.tsx`,
  `frontend/src/components/AlwaysOnControl.tsx`,
  `frontend/src/utils/recordingPresence.ts` (if cross-surface
  flag needs broadening).

### SR-008 [M] Frontend SessionDetails Video tab
- **Scope**: New "Video" tab in `SessionDetails.tsx`,
  conditional on `session.video_url || session.is_local`.
  Native `<video>` player. Transcript click -> seek. Chapters
  from VAD segment boundaries (first 6-10 words as title).
  Download buttons (WebM, MP4 when present, poster). Local-
  only path reads chunks from IndexedDB and concatenates
  into a Blob URL.
- **Deps**: SR-003.
- **Files**: `frontend/src/pages/SessionDetails.tsx`
  (new `VideoTab` component inline or split out as
  `frontend/src/components/SessionVideoTab.tsx`).

### SR-009 [S] Settings UI: video defaults + retention
- **Scope**: New "Video" section in Settings page. Toggle
  H.264 transcode default. Display per-org retention
  (read-only Phase 1, editable Phase 2). Toggle "include
  poster.jpg in downloads." Frame-rate / resolution /
  bitrate selectors (Phase 1 ships defaults only;
  selectors are stubbed out for Phase 2).
- **Deps**: SR-001, SR-008.
- **Files**: `frontend/src/pages/SettingsEnhanced.tsx`,
  `frontend/src/components/settings/VideoSettings.tsx` (new).

### SR-010 [M] Garage bucket setup + lifecycle policy
- **Scope**: Garage admin work — create `meeting-video`
  bucket, set lifecycle rule to delete objects under
  `org-*/session-*/chunks/` after 30 days, configure access
  keys for the backend. Add documentation under
  `docs/garage-bucket-setup.md`.
- **Deps**: none (infra ticket, can run in parallel).
- **Files**: `docs/garage-bucket-setup.md` (new),
  `backend/.env.example`.

### SR-011 [M] Retention sweeper for video
- **Scope**: Extend `services/retention_sweeper.py` (CR-008
  if landed; otherwise new) to also process
  `recording_sessions.video_retention_expires_at`. Calls
  `garage_client.delete_prefix()` for the session's video
  prefix. Skips pinned sessions. Audit-logs every deletion.
  Runs nightly via APScheduler with Postgres job store.
- **Deps**: SR-002, SR-001. Soft-dep on CR-008 (if both
  land in the same window, share the sweeper service).
- **Files**: `backend/services/retention_sweeper.py`.

### SR-012 [M] Per-org video quota enforcement
- **Scope**: Quota check in `POST /video-chunks` (reject
  413 if org over `video_storage_quota_mb`). Compute usage
  by `SUM(bytes) FROM video_chunks WHERE
  session.organization_id = X`. Cache result with 60 s TTL
  to avoid scanning every chunk POST. Daily recompute job
  to correct drift.
- **Deps**: SR-003, SR-001.
- **Files**: `backend/services/video_quota.py` (new),
  `backend/api/recording_video.py`.

### SR-013 [S] Documentation + sample configs
- **Scope**: Update `README.md` + `INSTALL.md` with a
  Screen Recording section. Add quickstart at
  `docs/screen-recording-quickstart.md`. Document env
  vars in `.env.example`. CHANGELOG entry.
- **Deps**: SR-007, SR-008.
- **Files**: `README.md`, `INSTALL.md`,
  `docs/screen-recording-quickstart.md` (new),
  `backend/.env.example`.

### SR-014 [M] Browser compatibility detection + UX gating
- **Scope**: `isScreenVideoCaptureSupported()` helper.
  Toggle hidden (not just disabled) on unsupported
  browsers. Friendly "Screen recording requires Chrome
  or Edge on desktop" tooltip in the Settings -> Video
  panel. No support detection for iOS / Android — toggle
  is hidden.
- **Deps**: SR-005, SR-007.
- **Files**: `frontend/src/services/screenCapture.ts`,
  `frontend/src/components/AlwaysOnControl.tsx`,
  `frontend/src/components/settings/VideoSettings.tsx`.

## 20. Success Criteria

Phase 1 is "done" when a Chrome user can:

1. Open `/record/personal`, see a "Record screen too" toggle, flip
   it on.
2. Click Start. Browser shows the OS screen-share picker. User
   picks a tab, window, or entire screen and shares.
3. The session status flips to "Recording". The audio source
   indicator shows the existing mic/tab/mic+tab mode. A new
   "Recording screen" pill appears next to it. The IndexedDB
   storage estimate shows in the control panel.
4. The session runs as long as the user wants. Chunks upload in
   the background. The user can navigate away and come back —
   the session resumes (audio chunks continue uploading, video
   chunks continue uploading).
5. The user clicks Stop. The video pipeline flushes the final
   chunk, the audio pipeline finalizes. Server runs
   `/finalize-video` in the background.
6. Within a couple of minutes, SessionDetails shows a Video tab
   with the native player.
7. Clicking a transcript line seeks the video. Chapters from VAD
   boundaries appear in the player.
8. The user can download the WebM. If H.264 transcode is on,
   they can also download the MP4.
9. Storage shows up in the org's quota (Phase 1 ships the
   counter; UI lands Phase 2).
10. After 30 days, the Garage lifecycle policy deletes the
    chunks + assembled video + poster. The session row
    flips `video_status='expired'` and the Video tab
    disappears. Audio + transcript stay forever (their
    retention is governed separately).

If all 10 succeed without disturbing any existing always-on or
conference room behavior, Phase 1 ships.
