# Always-On Recording with Auto-Segmentation

## Overview

Continuous recording mode where the system listens indefinitely and automatically segments audio into discrete meetings based on silence gaps and (optionally) contextual analysis.

## Architecture

### Phase 1: Silence-Gap Segmentation (Recommended First)

```
┌─────────────────────────────────────────────────────┐
│                 Always-On Recorder                   │
│                                                      │
│  ┌──────────┐    ┌──────────────┐    ┌───────────┐  │
│  │ PipeWire │───>│ VAD (Voice   │───>│ Segment   │  │
│  │ ffmpeg   │    │ Activity     │    │ Manager   │  │
│  │ capture  │    │ Detection)   │    │           │  │
│  └──────────┘    └──────────────┘    └─────┬─────┘  │
│                                            │        │
│                    ┌───────────────────────┐│        │
│                    │ When speech starts:   ││        │
│                    │  → Create new session ││        │
│                    │  → Start recording    │▼        │
│                    │ When silence > 5min:  │         │
│                    │  → Stop recording     │         │
│                    │  → Finalize session   │         │
│                    │  → Process transcript │         │
│                    └───────────────────────┘         │
└─────────────────────────────────────────────────────┘
```

**How it works:**

1. System runs a single long-lived ffmpeg process capturing from PipeWire
2. Audio is analyzed in 15-second chunks (same as current live transcription)
3. `_chunk_has_speech()` (already exists, -75dB threshold) determines voice activity
4. State machine:
   - **IDLE**: No speech detected. Waiting for voice activity.
   - **RECORDING**: Speech detected. Recording to WAV, transcribing live.
   - **SILENCE_GAP**: Speech stopped. Counting silent chunks (up to threshold).
   - **FINALIZING**: Silence threshold reached. Stopping recording, processing.

**State transitions:**

```
IDLE ──(speech detected)──> RECORDING
RECORDING ──(silence)──> SILENCE_GAP
SILENCE_GAP ──(speech resumes)──> RECORDING  (same session continues)
SILENCE_GAP ──(threshold reached)──> FINALIZING ──> IDLE
```

**Configuration:**
- `silence_threshold_minutes`: 5 (default) — how long to wait before splitting
- `min_meeting_duration_seconds`: 30 — ignore very short speech bursts (coughs, etc.)
- `speech_threshold_db`: -75 (current default)

### Phase 2: LLM Context Splitting (Enhancement)

After a silence gap triggers a potential split, send the last ~200 words before the gap and first ~200 words after to Granite 3.3 2B:

```
System: You analyze meeting transcripts. Given text before and after a silence gap,
determine if this is the SAME meeting resuming or a NEW meeting starting.
Respond with exactly: SAME or NEW

User:
BEFORE GAP: "...and we'll follow up on the budget next week. Thanks everyone."
[5 minute silence]
AFTER GAP: "Good morning everyone, let's talk about the Q3 hiring plan..."
```

If LLM says "SAME", merge the segments. If "NEW", keep them split.

This is a refinement — Phase 1 silence-gap splitting will be correct 90%+ of the time.

### Phase 3: Multi-Room / Satellite Devices (Future)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ Room A       │     │ Room B       │     │ Browser      │
│ USB Mic      │     │ Satellite    │     │ Extension    │
│ (local)      │     │ Device       │     │ (WebRTC)     │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       │    ┌───────────────┴────────────────────┘
       │    │
       ▼    ▼
┌──────────────────────┐
│  Meeting-Ops Server  │
│  Audio Ingestion API │
│  POST /api/simple/   │
│  recording-sessions/ │
│  {id}/upload-audio   │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Transcription +     │
│  Diarization +       │
│  AI Summarization    │
└──────────────────────┘
```

The audio upload endpoint already exists (`api/audio_upload.py`). Satellite devices would:
1. Record locally (even offline)
2. Upload WAV/FLAC to the server when connected
3. Server processes and creates sessions automatically

## Implementation Plan

### Phase 1 Implementation (silence-gap splitting)

**New file: `backend/services/always_on_recorder.py`**

```python
class AlwaysOnRecorder:
    """
    Continuous recording service with automatic meeting segmentation.
    Listens on the configured mic indefinitely. When speech is detected,
    starts a new recording session. When silence exceeds the threshold,
    finalizes the session and waits for the next meeting.
    """

    def __init__(self):
        self.state = "IDLE"  # IDLE, RECORDING, SILENCE_GAP, FINALIZING
        self.silence_threshold_chunks = 20  # 5 min at 15s chunks
        self.min_meeting_chunks = 2  # 30s minimum
        self.current_session_id = None
        self.consecutive_silent = 0
        self.speech_chunks = 0

    async def start(self, device_id=None):
        """Start always-on monitoring"""
        # Long-lived ffmpeg capture process
        # Continuous chunk analysis loop

    async def _on_speech_start(self):
        """Create new session, start recording + live transcription"""

    async def _on_meeting_end(self):
        """Stop recording, finalize session, trigger post-processing"""
```

**Modified files:**
- `api/simple_recording_db.py` — add endpoints:
  - `POST /api/simple/always-on/start` — start always-on mode
  - `POST /api/simple/always-on/stop` — stop always-on mode
  - `GET /api/simple/always-on/status` — current state + active session
- `frontend/src/pages/LiveRecording.tsx` — add toggle for "Always-On Mode"

**Estimated complexity:** Medium. Core logic is ~200 lines. Reuses existing `_chunk_has_speech()`, `audio_service.start_recording()`, `live_recording_transcription`, and session creation endpoints.

### Phase 2 Implementation (LLM context splitting)

**Modified files:**
- `always_on_recorder.py` — add `_check_context_continuity()` method
- Calls `llm_service._call_llm()` with before/after text

**Estimated complexity:** Low (adds ~50 lines to Phase 1).

### Phase 3 Implementation (multi-room / satellite)

**New work needed:**
- Satellite device firmware/software (Raspberry Pi or ESP32)
- Browser extension with MediaRecorder API
- Device registry + room management in DB
- Upload queue with retry logic

**Estimated complexity:** High. This is a separate project phase.

## Dependencies

- Phase 1 depends on: auto-stop (already implemented), silence detection (already exists)
- Phase 2 depends on: Phase 1 + LLM service (already working)
- Phase 3 depends on: Phase 1 + audio upload API (already exists)

## Open Questions

1. Should always-on mode auto-start on boot? (Probably yes for appliance use case)
2. Maximum concurrent sessions? (Probably 1 for single-mic, N for multi-room)
3. Should the UI show a persistent "listening..." indicator when in always-on mode?
4. Storage management — auto-delete recordings older than N days?
