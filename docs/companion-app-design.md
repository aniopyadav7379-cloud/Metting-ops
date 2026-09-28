# Companion App Design Document

## Overview

The Companion App is a lightweight menu bar / system tray application that captures microphone and system audio from remote machines (primarily Mac Studio) and streams it to the Meeting-Ops server for transcription, summarization, and AI analysis. All heavy processing (Whisper STT, speaker diarization, LLM summarization, AI insights) stays on the server. The client is intentionally thin: its only jobs are audio capture, mixing, and reliable WebSocket streaming.

## Platform Options

### macOS (Primary - Swift native)

The primary target is macOS on Apple Silicon, built as a native Swift app.

- **System audio capture**: Core Audio Taps API (macOS 14.2+) for direct system audio capture without virtual audio devices. Falls back to ScreenCaptureKit (macOS 13+) on older systems.
- **Microphone capture**: AVAudioEngine with AVAudioSession for low-latency mic input.
- **UI**: Menu bar app using SwiftUI. No dock icon, no main window. Lives in the menu bar with a dropdown showing session status, audio levels, and start/stop controls.
- **Permissions**: Requires Microphone permission and Screen Recording permission (for system audio via ScreenCaptureKit). Core Audio Taps requires the `com.apple.security.audio.capture` entitlement.
- **Footprint**: Native binary under 10MB, minimal CPU usage (~2-3% during capture), no runtime dependencies.
- **Reference implementation**: [AudioCap](https://github.com/insidegui/AudioCap) demonstrates Core Audio Taps usage for system audio capture on macOS 14.2+.

Advantages:
- Best system integration and native permissions flow
- Smallest binary size and memory footprint
- Direct access to Core Audio Taps (no wrappers)
- Keychain integration for secure JWT storage
- Launch-at-login via SMLoginItemSetEnabled

### Electron (Cross-platform alternative)

If cross-platform support is needed before native apps exist for each platform, Electron provides a single codebase option.

- **Microphone capture**: `navigator.mediaDevices.getUserMedia()` with Web Audio API for processing.
- **System audio (macOS)**: [AudioTee.js](https://github.com/makeusabrew/audioteejs) wraps Core Audio Taps as a native Node addon. Alternatively, `electron-audio-loopback` uses Chromium's built-in ScreenCaptureKit integration via `desktopCapturer`.
- **System audio (Windows/Linux)**: Not available through standard Electron APIs. Would require native addons (WASAPI loopback on Windows, PipeWire monitor on Linux).
- **UI**: System tray app using Electron's `Tray` API with a popup window.
- **Binary size**: ~150MB due to bundled Chromium runtime.

Advantages:
- Single codebase for Mac/Windows/Linux (mic capture only on non-Mac)
- Familiar web tech stack (TypeScript, React)
- Faster initial development if team lacks Swift experience

Disadvantages:
- 150MB+ binary vs <10MB native
- Higher CPU and memory usage (~100MB RAM idle)
- System audio on macOS requires native addon anyway
- No system audio on Windows/Linux without additional native code

### Future: Windows

- **System audio**: WASAPI loopback capture provides direct access to system audio output
- **Microphone**: WASAPI or Windows.Media.Capture APIs
- **UI**: System tray app via WinUI 3 or WPF
- **Auth**: Windows Credential Manager for JWT storage

### Future: Linux

- **System audio**: PipeWire monitor source or PulseAudio `monitor` device
- **Microphone**: PipeWire/PulseAudio or direct ALSA
- **UI**: System tray via libappindicator or Qt
- **Auth**: libsecret / GNOME Keyring for JWT storage

## Architecture

```
Client (Mac/PC)                    Meeting-Ops Server (Linux)
+---------------------------+      +---------------------------+
| Companion App             |      | FastAPI Backend (9050)    |
|                           | WSS  |                           |
| [Mic] -> capture ---------|----->| /ws/remote-audio/{id}     |
| [System Audio] -> capture-|----->|   -> WAV file write       |
|                           |      |   -> Whisper STT          |
| [Session CRUD] -----------|----->|   -> Progressive Summary  |
| [Auth] -------------------|----->|   -> Speaker Diarization  |
+---------------------------+      +---------------------------+
```

### Client-Side Components

```
CompanionApp
  |
  +-- AudioCaptureManager
  |     +-- MicCapture (AVAudioEngine / getUserMedia)
  |     +-- SystemAudioCapture (Core Audio Taps / ScreenCaptureKit)
  |     +-- AudioMixer (combines mic + system, resamples to 16kHz mono)
  |
  +-- StreamingManager
  |     +-- WebSocketClient (binary PCM frame transport)
  |     +-- ReconnectionHandler (exponential backoff, local buffer)
  |     +-- ChunkBuffer (ring buffer for in-flight frames)
  |
  +-- SessionManager
  |     +-- REST client for session CRUD
  |     +-- Session state machine (idle -> recording -> stopping -> stopped)
  |
  +-- AuthManager
  |     +-- JWT token management
  |     +-- Keychain / credential store integration
  |     +-- Token refresh logic
  |
  +-- MenuBarUI (SwiftUI / Tray)
        +-- Audio level meters (mic + system)
        +-- Session name input
        +-- Start / Stop controls
        +-- Connection status indicator
        +-- Recent sessions list
```

### Server-Side Components (new endpoints in Meeting-Ops)

```
/ws/remote-audio/{session_id}
  |
  +-- Authentication (JWT validation from query param or first message)
  +-- PCM Frame Receiver
  |     +-- Frame reassembly and validation
  |     +-- WAV file progressive writer
  |
  +-- Transcription Pipeline (existing)
  |     +-- Whisper STT (NPU-accelerated)
  |     +-- Speaker diarization
  |
  +-- Summary Pipeline (existing)
        +-- Progressive summarization via unified_agent_service
        +-- AI insights generation
```

## Audio Specification

| Parameter | Value | Notes |
|-----------|-------|-------|
| Format | 16-bit signed PCM (little-endian) | Raw samples, no container headers |
| Sample rate | 16,000 Hz | Whisper's native rate, avoids server-side resampling |
| Channels | 1 (mono) | Mic and system audio mixed on client before sending |
| Bit depth | 16 bits | 2 bytes per sample |
| Chunk duration | 200-500ms | Balances latency vs overhead |
| Chunk size | 6,400-16,000 bytes | At 16kHz mono 16-bit: 32,000 bytes/sec |
| Bandwidth | ~256 kbps | 32,000 bytes/sec raw, negligible for any network |

### Transport Protocol

- **WebSocket binary frames** over WSS (TLS required in production)
- Each WebSocket message contains one chunk of raw PCM data
- No additional framing or headers within the binary payload
- Text messages reserved for control commands (JSON):
  - `{"type": "start", "sample_rate": 16000, "channels": 1, "bit_depth": 16}`
  - `{"type": "stop"}`
  - `{"type": "ping"}` / `{"type": "pong"}`
  - `{"type": "status"}` -> server replies with session state

### Authentication

JWT token passed in one of two ways (in priority order):
1. WebSocket URL query parameter: `/ws/remote-audio/{session_id}?token=<jwt>`
2. First WebSocket text message: `{"type": "auth", "token": "<jwt>"}`

The server must validate the token before accepting any audio frames. Invalid or expired tokens result in WebSocket close with code 4401.

### Audio Mixing (Client-Side)

When both mic and system audio are captured simultaneously:
1. Both streams are resampled to 16kHz mono
2. Samples are mixed by averaging: `mixed[i] = (mic[i] + system[i]) / 2`
3. Clipping protection applied (clamp to INT16 range)
4. Mixed stream is chunked and sent over WebSocket

The client may optionally send mic and system audio as separate streams over two WebSocket connections to the same session, allowing the server to store them independently for better speaker diarization. This is a Phase 4 enhancement.

## Backend Requirements (to be built in Meeting-Ops)

### New WebSocket Endpoint

**File**: `backend/api/websocket_remote_audio.py`

```python
# WebSocket endpoint: /ws/remote-audio/{session_id}
# Accepts binary PCM frames from companion app
# Writes to WAV file progressively
# Feeds audio chunks to existing Whisper pipeline
```

Key behaviors:
- Validate JWT token on connection
- Verify session exists and belongs to authenticated user
- Accept binary WebSocket messages as raw PCM frames
- Write PCM data to WAV file progressively (write header on first frame, update header on close)
- Forward audio chunks to `real_whisper_service` for transcription (same pipeline as local recording)
- Forward transcript chunks to `unified_agent_service` for progressive summarization
- Handle WebSocket disconnection gracefully (finalize WAV header, mark session as paused)
- Support reconnection (append to existing WAV file, resume transcription)

### New Router Registration

Add `websocket_remote_audio` to the optional routers list in `backend/main.py`.

### Session Metadata

Add a `source` field to recording sessions to distinguish local vs remote recordings:
- `local` - recorded from server hardware mic
- `remote` - streamed from companion app
- Store client info (hostname, OS, app version) in session metadata

### Reconnection Protocol

1. Client detects WebSocket disconnection
2. Client buffers audio locally (ring buffer, configurable max size, default 60 seconds)
3. Client reconnects with exponential backoff (1s, 2s, 4s, 8s, max 30s)
4. On reconnect, client sends: `{"type": "resume", "last_timestamp_ms": 12345}`
5. Server responds with: `{"type": "resume_ack", "expected_timestamp_ms": 12345}`
6. Client sends buffered audio, then switches to live streaming
7. If buffer overflows during disconnection, client sends: `{"type": "gap", "duration_ms": 5000}` to indicate missing audio

## Session Flow

### 1. Authentication

```
Companion App                          Meeting-Ops Server
     |                                        |
     |  POST /api/auth/login                  |
     |  {"username": "admin",                 |
     |   "password": "admin123"}              |
     |--------------------------------------->|
     |                                        |
     |  200 {"access_token": "eyJ...",        |
     |       "token_type": "bearer"}          |
     |<---------------------------------------|
     |                                        |
     |  Store JWT in Keychain                 |
     |                                        |
```

### 2. Session Creation

```
Companion App                          Meeting-Ops Server
     |                                        |
     |  POST /api/simple/recording-sessions   |
     |  {"title": "Weekly Standup",           |
     |   "source": "remote",                  |
     |   "client_info": {                     |
     |     "hostname": "mac-studio",          |
     |     "os": "macOS 15.2",               |
     |     "app_version": "1.0.0"}}           |
     |--------------------------------------->|
     |                                        |
     |  201 {"id": "uuid-...",                |
     |       "status": "created"}             |
     |<---------------------------------------|
```

### 3. Audio Streaming

```
Companion App                          Meeting-Ops Server
     |                                        |
     |  WS /ws/remote-audio/{session_id}      |
     |    ?token=eyJ...                       |
     |--------------------------------------->|
     |                                        |
     |  Text: {"type": "start",               |
     |         "sample_rate": 16000,          |
     |         "channels": 1,                 |
     |         "bit_depth": 16}               |
     |--------------------------------------->|
     |                                        |
     |  Text: {"type": "start_ack"}           |
     |<---------------------------------------|
     |                                        |
     |  Binary: [PCM frame 1 - 6400 bytes]    |
     |--------------------------------------->|
     |  Binary: [PCM frame 2 - 6400 bytes]    |
     |--------------------------------------->|
     |  ...continues...                       |
     |                                        |
     |  Text: {"type": "transcript",          |
     |         "text": "Good morning...",     |
     |         "timestamp": 0.0}              |
     |<---------------------------------------|
     |                                        |
```

### 4. Stop Recording

```
Companion App                          Meeting-Ops Server
     |                                        |
     |  Text: {"type": "stop"}                |
     |--------------------------------------->|
     |                                        |
     |  Text: {"type": "stopped",             |
     |         "duration_seconds": 1847,      |
     |         "transcript_segments": 42}     |
     |<---------------------------------------|
     |                                        |
     |  WebSocket closed (1000 Normal)        |
     |                                        |
```

### 5. Post-Recording

After stopping, the server finalizes:
- WAV file header with correct data length
- Final transcription pass (if any buffered audio remains)
- Session summary generation via LLM
- AI insights (keywords, sentiment, action items)
- Session status updated to `completed`

The user can then view the full transcript, summary, and insights in the Meeting-Ops web UI at `/sessions/{id}`.

## MVP Phases

### Phase 1: Backend Remote Audio WebSocket Endpoint

**Goal**: Accept streamed PCM audio from any WebSocket client and process it through the existing pipeline.

- Implement `/ws/remote-audio/{session_id}` WebSocket endpoint
- JWT authentication on connection
- Progressive WAV file writing from binary PCM frames
- Integration with existing Whisper transcription pipeline
- Integration with existing progressive summarization
- Register router in `main.py`
- Test with a simple Python WebSocket client script

**Deliverable**: Working backend endpoint tested with `websocat` or Python script.

### Phase 2: macOS Swift App (Mic Only)

**Goal**: Native menu bar app that captures mic audio and streams to Meeting-Ops.

- SwiftUI menu bar app (no dock icon)
- Login screen with server URL, username, password
- JWT stored in macOS Keychain
- Session creation (name input + start button)
- AVAudioEngine mic capture at 16kHz mono 16-bit PCM
- WebSocket streaming with URLSessionWebSocketTask
- Audio level meter in menu bar dropdown
- Stop button finalizes session
- Basic error handling and connection status display

**Deliverable**: Functional mic-only companion app for macOS.

### Phase 3: macOS Swift App (Mic + System Audio)

**Goal**: Add system audio capture alongside mic audio.

- Core Audio Taps API integration (macOS 14.2+)
- ScreenCaptureKit fallback (macOS 13+)
- Audio mixing (mic + system audio)
- Separate volume controls for mic and system audio
- Permission request flows for Screen Recording
- Audio source selection (choose which mic, choose which app/system for system audio)

**Deliverable**: Full audio capture companion app for macOS.

### Phase 4: Polish and Reliability

**Goal**: Production-ready reliability and user experience.

- Reconnection with local audio buffering (60-second ring buffer)
- Exponential backoff on disconnect
- Resume protocol (gap detection, timestamp sync)
- Audio level meters for both mic and system audio
- Session history list in menu bar dropdown
- Launch at login option
- Auto-update mechanism (Sparkle framework)
- Notification when session finishes processing
- Keyboard shortcuts for start/stop
- Configurable audio chunk size and quality settings

**Deliverable**: Polished, reliable companion app.

### Phase 5: Cross-Platform

**Goal**: Companion apps for Windows and Linux.

Option A (native per platform):
- Windows: WinUI 3 app with WASAPI loopback
- Linux: Qt/GTK app with PipeWire monitor source

Option B (Electron):
- Single Electron app for all platforms
- Native audio addons per platform for system audio
- Mic capture via standard Web Audio API

**Deliverable**: Companion app available on all major desktop platforms.

## References

- **Apple Core Audio Taps**: [developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps](https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps)
- **AudioCap sample code**: [github.com/insidegui/AudioCap](https://github.com/insidegui/AudioCap)
- **AudioTee.js** (Core Audio Taps Node addon): [github.com/makeusabrew/audioteejs](https://github.com/makeusabrew/audioteejs)
- **ScreenCaptureKit**: [developer.apple.com/documentation/screencapturekit](https://developer.apple.com/documentation/screencapturekit)
- **AVAudioEngine**: [developer.apple.com/documentation/avfaudio/avaudioengine](https://developer.apple.com/documentation/avfaudio/avaudioengine)
- **WASAPI Loopback**: [learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
- **PipeWire**: [pipewire.org](https://pipewire.org)
