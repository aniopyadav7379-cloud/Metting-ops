# Satellite Devices: Multi-Room Recording Architecture

## Overview

Meeting-Ops supports multi-room recording via satellite devices -- lightweight hardware nodes (ESP32-S3, Raspberry Pi) or companion software -- that capture audio in separate rooms and send it to the central Meeting-Ops server for transcription, summarization, and storage. This enables a single server to manage recordings across an entire office with minimal per-room cost.

### Key Principles

- **Central brain, dumb edges**: The server runs Whisper, LLM summarization, and storage. Satellite devices only capture and stream audio (with optional local STT on RPi).
- **Graceful degradation**: Satellites that lose WiFi can record locally and upload later (store-and-forward).
- **Unified pipeline**: Audio from satellites flows through the same transcription and AI pipeline as local mic recordings.
- **Zero-touch discovery**: Devices announce themselves via mDNS and auto-register with the server.

---

## Device Types

### ESP32-S3 Satellite (~$15/node)

The lowest-cost option. An ESP32-S3 microcontroller with an I2S MEMS microphone captures audio and streams it to the server over WiFi.

| Component | Part | Est. Cost |
|-----------|------|-----------|
| MCU | ESP32-S3 DevKit (N8R2 or N16R8) | $6-10 |
| Microphone | INMP441 I2S MEMS mic breakout | $2-3 |
| Storage | microSD card module (SPI) + 8GB card | $1-2 |
| Enclosure | 3D-printed or off-the-shelf project box | $1-2 |
| Power | USB-C cable + 5V adapter | $3-5 |
| **Total** | | **$13-22** |

**Capabilities:**
- Records 16 kHz / 16-bit / mono PCM audio via I2S DMA
- Streams audio in real time over WiFi WebSocket (binary PCM frames)
- Records locally to microSD card as WAV files (store-and-forward mode)
- Physical button to start/stop recording
- LED status indicator (idle / recording / streaming / error)
- mDNS auto-discovery: `_meetingops-satellite._tcp.local`

**Operating Modes:**
1. **Real-time streaming** (primary): Opens WebSocket to `ws://<server>:9050/ws/satellite/{device_id}/audio` and sends raw PCM frames as they are captured. Server performs all processing.
2. **Store-and-forward**: Records audio to microSD as WAV files named `{device_id}_{timestamp}.wav`. When WiFi is available, uploads each file via `POST /api/satellites/{device_id}/upload-audio`. Deletes local file after successful upload + server acknowledgment.

**Firmware:**
- Arduino framework with ESP-IDF backend
- Libraries: `arduino-audio-tools` (I2S + WAV encoding), `ArduinoWebsockets`, `SD`, `ESPmDNS`
- OTA firmware updates via HTTP endpoint on the server (future)

**Wiring (INMP441 to ESP32-S3):**
```
INMP441 Pin    ESP32-S3 Pin
-----------    ------------
SCK (BCLK)     GPIO 5
WS (LRCLK)     GPIO 6
SD (DATA)       GPIO 7
L/R             GND (left channel)
VDD             3.3V
GND             GND
```

---

### Raspberry Pi Satellite (~$130/node)

A more capable option that can optionally run local speech-to-text, voice activity detection, and audio preprocessing before sending data to the server.

| Component | Part | Est. Cost |
|-----------|------|-----------|
| SBC | Raspberry Pi 5 (8GB) | $80 |
| Microphone | USB conference mic or ReSpeaker 2-Mic HAT | $15-25 |
| Storage | 32GB microSD (A2) | $8 |
| Power | Official RPi 5 27W USB-C PSU | $12 |
| Case | Official RPi 5 case with fan | $10 |
| **Total** | | **$125-135** |

**Capabilities:**
- Records 16 kHz / 16-bit / mono audio via USB mic or ALSA HAT
- Can run `whisper.cpp` locally (base.en model, ~3-5x realtime on RPi 5)
- Voice Activity Detection (VAD) via Silero or webrtcvad
- Audio preprocessing: noise gate, gain normalization
- Full Linux networking stack (WiFi, Ethernet, mDNS)

**Operating Modes:**
1. **Dumb streaming**: Same as ESP32 -- streams raw PCM to server WebSocket. Server does all processing.
2. **VAD + stream**: Runs local VAD. Only streams audio when speech is detected, saving bandwidth and server processing.
3. **Local STT**: Runs whisper.cpp locally, sends completed transcript segments to `POST /api/satellites/{device_id}/transcript`. Server only runs LLM summarization. Ideal for bandwidth-constrained environments.

**Software:**
- Python 3 service using `sounddevice` or `arecord` for audio capture
- WebSocket client (`websockets` library) for streaming mode
- `whisper.cpp` Python bindings for local STT mode
- systemd service for auto-start on boot
- Configuration via `/etc/meetingops-satellite/config.yaml`

---

### Generic Network Mic / Companion App (future)

Any device or application that can POST audio files or stream via WebSocket can act as a satellite:
- **Companion apps**: Mac/PC/phone apps that capture system audio or mic input and stream to the server
- **IP conference phones**: Devices that support SIP recording or audio export
- **Other IoT**: Any device with a mic and network stack

These use the same `POST /api/satellites/{device_id}/upload-audio` or WebSocket streaming endpoints.

---

## Communication Protocols

### 1. WebSocket Streaming (Primary)

**Endpoint:** `WS /ws/satellite/{device_id}/audio`

Real-time binary audio streaming from device to server.

**Connection flow:**
```
Device                          Server
  |                               |
  |--- WS CONNECT --------------->|
  |    (with device_id in path)   |
  |                               |--- validate device_id
  |                               |--- create RecordingSession
  |                               |--- open WAV file writer
  |<-- TEXT {"session_id": "..."}-|
  |                               |
  |--- BINARY (PCM frame) ------->|  (every ~100ms, 1600 samples)
  |--- BINARY (PCM frame) ------->|
  |--- BINARY (PCM frame) ------->|
  |         ...                   |--- every 15s: feed chunk to Whisper
  |                               |--- every 15s: broadcast transcript via Redis
  |                               |
  |--- WS CLOSE ----------------->|
  |                               |--- finalize WAV
  |                               |--- trigger process_recording()
```

**Audio format:** Raw 16-bit signed little-endian PCM, 16000 Hz, mono. Each WebSocket binary frame contains 1600 samples (100ms) = 3200 bytes.

**Reconnection:** If the same device_id reconnects within 60 seconds of disconnect, the server appends to the existing session rather than creating a new one.

### 2. HTTP Upload (Store-and-Forward)

**Endpoint:** `POST /api/satellites/{device_id}/upload-audio`

For devices that recorded locally (e.g., ESP32 with SD card) and are uploading after the fact.

**Request:** `multipart/form-data` with:
- `file`: WAV audio file
- `recorded_at` (optional): ISO 8601 timestamp of when recording started
- `session_id` (optional): Existing session ID to append to
- `room_name` (optional): Override room name for this recording

**Response:**
```json
{
  "session_id": "abc-123",
  "status": "processing",
  "message": "Audio uploaded, transcription queued"
}
```

**Server behavior:**
1. Saves WAV file to recordings directory
2. Creates a new RecordingSession (or uses provided session_id)
3. Sets `source_type = "satellite_upload"` and `source_device_id = device_id`
4. Triggers `process_recording()` pipeline (Whisper + LLM)

### 3. Transcript Upload (RPi Local STT)

**Endpoint:** `POST /api/satellites/{device_id}/transcript`

For Raspberry Pi satellites that ran local whisper.cpp and are sending completed transcripts.

**Request body:**
```json
{
  "transcript_text": "Full transcript text...",
  "segments": [
    {
      "text": "Hello everyone, let's get started.",
      "speaker": "SPEAKER_00",
      "start": 0.0,
      "end": 2.5,
      "confidence": 0.92
    }
  ],
  "duration": 3600.0,
  "recorded_at": "2026-02-22T10:00:00Z",
  "audio_file_available": true
}
```

**Server behavior:**
1. Creates RecordingSession with transcript pre-populated
2. Saves individual transcript segments to `transcriptions` table
3. Triggers LLM summarization pipeline only (skips Whisper)
4. Sets `source_type = "satellite_transcript"` and `source_device_id = device_id`

### 4. Heartbeat

**Endpoint:** `POST /api/satellites/{device_id}/heartbeat`

Devices send a heartbeat every 30 seconds to indicate they are alive and connected.

**Request body (optional):**
```json
{
  "status": "recording",
  "battery_pct": 85,
  "wifi_rssi": -42,
  "free_sd_mb": 7200
}
```

**Server behavior:**
- Updates `last_heartbeat` timestamp
- Updates `status` field
- Marks device as `offline` if no heartbeat received for 90 seconds (3x interval)

---

## Device Registration and Discovery

### Registration Flow

```
1. Device boots, connects to WiFi
2. Device announces via mDNS: _meetingops-satellite._tcp.local
3. Device discovers server via mDNS: _meetingops-server._tcp.local
   (or uses hardcoded server IP from config)
4. Device calls POST /api/satellites/register with:
   {
     "device_id": "esp32-office-001",
     "name": "Office Conference Room",
     "room_name": "Office",
     "device_type": "esp32-s3",
     "capabilities": {"audio": true, "sd_card": true, "local_stt": false, "vad": false},
     "firmware_version": "1.0.0",
     "ip_address": "192.168.1.50"
   }
5. Server returns API key for future authenticated requests
6. Device stores API key in NVS (ESP32) or config file (RPi)
7. Device begins sending heartbeats every 30 seconds
```

### Authentication

Satellites authenticate using one of:
- **JWT Bearer token**: Same as human users (for companion apps)
- **API key**: Generated at registration time, sent via `X-API-Key` header (for hardware devices)

The existing auth system already supports both methods via `get_current_user_optional` in `auth/dependencies.py`.

---

## Session Lifecycle

### Streaming Mode (ESP32/RPi)

```
1. Device connects to WS /ws/satellite/{device_id}/audio
2. Server creates RecordingSession with:
   - source_type = "satellite_stream"
   - source_device_id = device_id
   - room_name = device's configured room
   - status = "recording"
3. Device streams PCM frames
4. Server writes WAV file progressively
5. Every 15s: server feeds audio chunk to Whisper, stores segments
6. Device disconnects (button press, timeout, or error)
7. Server finalizes WAV file
8. Server runs process_recording() for final transcription + AI summary
9. Session status -> "completed"
```

### Store-and-Forward Mode (ESP32 with SD Card)

```
1. Physical button press on ESP32 starts recording
2. ESP32 records audio to microSD as {device_id}_{timestamp}.wav
3. Button press again stops recording
4. ESP32 checks WiFi connectivity
5. If connected: uploads WAV via POST /api/satellites/{device_id}/upload-audio
6. Server creates session, saves file, runs full pipeline
7. Server responds with 200 OK
8. ESP32 deletes local WAV file
9. If not connected: retries upload periodically (every 5 minutes)
```

### Local STT Mode (RPi)

```
1. RPi detects speech via VAD or receives start command
2. RPi records audio locally
3. RPi runs whisper.cpp on the audio file
4. RPi sends transcript via POST /api/satellites/{device_id}/transcript
5. Server creates session with transcript pre-populated
6. Server runs LLM summarization only (skips Whisper)
7. If audio_file_available=true, RPi also uploads audio via separate endpoint
```

---

## Database Schema

### New Table: satellite_devices

```sql
CREATE TABLE IF NOT EXISTS satellite_devices (
    id              SERIAL PRIMARY KEY,
    device_id       VARCHAR(100) UNIQUE NOT NULL,
    name            VARCHAR(200),
    room_name       VARCHAR(200),
    device_type     VARCHAR(50),        -- "esp32", "esp32-s3", "rpi4", "rpi5", "companion-app"
    capabilities    TEXT,                -- JSON: {"audio": true, "local_stt": false, "vad": false, "sd_card": true}
    ip_address      VARCHAR(50),
    last_heartbeat  TIMESTAMP,
    status          VARCHAR(50) DEFAULT 'offline',  -- "online", "offline", "recording", "uploading", "error"
    firmware_version VARCHAR(50),
    config          TEXT,                -- JSON: {"sample_rate": 16000, "bit_depth": 16, "channels": 1, "stream_mode": "websocket"}
    api_key         VARCHAR(200),        -- API key for device authentication
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP
);
```

### Extended: recording_sessions (new columns)

```sql
ALTER TABLE recording_sessions
    ADD COLUMN IF NOT EXISTS source_device_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) DEFAULT 'local_mic',
    ADD COLUMN IF NOT EXISTS room_name VARCHAR(200);
```

- `source_device_id`: The `device_id` of the satellite that recorded this session (NULL for local mic).
- `source_type`: One of `"local_mic"`, `"satellite_stream"`, `"satellite_upload"`, `"satellite_transcript"`, `"companion_app"`.
- `room_name`: Physical room where the recording took place.

---

## API Endpoints Summary

| Method | Endpoint | Purpose | Auth |
|--------|----------|---------|------|
| POST | `/api/satellites/register` | Register new satellite device | JWT/API-Key |
| GET | `/api/satellites` | List all satellite devices | JWT |
| GET | `/api/satellites/{device_id}` | Get satellite device details | JWT |
| PUT | `/api/satellites/{device_id}` | Update satellite config | JWT |
| DELETE | `/api/satellites/{device_id}` | Remove satellite device | JWT |
| POST | `/api/satellites/{device_id}/heartbeat` | Device heartbeat | JWT/API-Key |
| POST | `/api/satellites/{device_id}/upload-audio` | Upload WAV (store-and-forward) | JWT/API-Key |
| POST | `/api/satellites/{device_id}/transcript` | Upload transcript (local STT) | JWT/API-Key |
| POST | `/api/satellites/{device_id}/start-recording` | Trigger recording on device | JWT |
| POST | `/api/satellites/{device_id}/stop-recording` | Stop recording on device | JWT |
| GET | `/api/satellites/rooms` | List rooms with assigned satellites | JWT |
| WS | `/ws/satellite/{device_id}/audio` | Real-time audio stream | Query param token |

---

## Security Considerations

1. **API keys**: Generated at registration, stored hashed in DB. Satellites send via `X-API-Key` header.
2. **Network isolation**: Satellites should be on a dedicated VLAN or WiFi SSID.
3. **TLS**: In production, all HTTP/WS traffic should use TLS (nginx reverse proxy with Let's Encrypt).
4. **Device revocation**: DELETE endpoint removes device and invalidates its API key.
5. **Rate limiting**: Heartbeat and upload endpoints should be rate-limited to prevent abuse.

---

## Future Enhancements

1. **OTA firmware updates**: Server hosts firmware binaries, ESP32 checks for updates at boot
2. **Multi-device sync**: Cross-room audio alignment for multi-room meetings that span rooms
3. **Beamforming**: ReSpeaker HAT on RPi for directional audio capture
4. **PoE powering**: RPi satellites powered via PoE HAT for single-cable deployment
5. **Device groups**: Group satellites by floor/building for batch management
6. **Audio routing**: Server tells satellite which WebSocket to connect to (load balancing)
7. **Companion apps**: Desktop/mobile apps that act as satellite devices for remote participants
