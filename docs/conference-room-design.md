# Conference Room Recording: Architecture Design

Status: Draft for approval. Implementation tickets at the end of this doc.
Owner: Meeting-Ops team.
Authoring date: 2026-05-20.

## 1. Purpose and Framing

Today UC-Meeting-Ops has exactly one recording surface that users interact with:
the browser. A user opens `/record`, clicks Always-On / Desktop Browser /
Mobile, and the laptop's mic does all the capture. The server only sees
finished audio chunks uploaded over HTTPS.

That surface is great for "I'm a person who wants to record a meeting I'm
attending right now." It's the wrong surface for "this room records itself,
forever, with hardware that nobody owns personally."

This doc designs the second surface — **Conference Room mode** — without
disturbing the first one. The two coexist:

| Mode | Who initiates | Where the mic is | Lifetime | Auth boundary |
|---|---|---|---|---|
| Personal / Browser | A user, per-meeting | Their device | Minutes-hours | The user |
| Conference Room | The room itself, on schedule / event / button | Physical hardware bound to the room | Indefinite | The room + room ACL |

Aaron's verbatim framing: *"for rooms where the server has a physical mic or
a satellite device that sends audio to the server or whatever, like for
permanent or semi-permanent conference recording type thing."*

The design goals are:

1. **Rooms are first-class.** A Conference Room is a database entity with
   admin lifecycle, ACL, retention policy, and a hardware binding — not a
   user with a fake browser.
2. **Multiple hardware paths converge to one pipeline.** Server-attached
   USB (one or many), and ESP32 / RPi / generic RoomHost satellites all
   land in the same `/chunks-text` + `/chunks` ingestion path the
   browser already uses — just with a `room_id` field instead of being
   user-driven. No bespoke streaming protocol; rooms POST chunks like
   any other client.
3. **Don't break what works.** AlwaysOnContext, DesktopBrowserRecorder, the
   `/api/recordings/start-always-on` chunk pipeline, and the mobile shell
   stay exactly as they are. Conference Room is additive.
4. **Reuse the existing satellite infrastructure.** `SatelliteDevice`
   table, `/api/satellites/*` endpoints, `/ws/satellite/.../audio`, and the
   `source_type='satellite_*'` extensions on `recording_sessions` already
   exist. This design wraps them in a "Room" abstraction rather than
   replacing them.

## 2. What Already Exists (Survey Result)

Before designing anything new, a survey of the codebase on `magicunicorn`
shows the following already in place:

### Backend

- `database/models.py::SatelliteDevice` — device registry table with
  `device_id`, `name`, `room_name`, `device_type`, `capabilities`,
  `ip_address`, `last_heartbeat`, `status`, `firmware_version`,
  `config`, `api_key`, `current_session_id`, `organization_id`
  (nullable today).
- `database/models.py::RecordingSession` extensions — `source_device_id`,
  `source_type` (with values `local_mic`, `satellite_stream`,
  `satellite_upload`, `satellite_transcript`, `companion_app`,
  `browser_always_on`), `room_name`.
- `api/satellite_api.py` (719 lines) — CRUD + heartbeat + audio-upload +
  transcript-upload + start/stop-recording. **Scoped by user only, NOT by
  organization** (see Section 12).
- `api/websocket_satellite.py` (409 lines) — `/ws/satellite/{device_id}/audio`
  WebSocket receiver that handles raw PCM streaming, WAV finalization,
  resumes within a 60-second reconnect window, and triggers
  `process_recording()` on disconnect.
- `api/websocket_remote_audio.py` (706 lines) — the companion-app
  equivalent for `/ws/remote-audio/{session_id}`.
- `services/always_on_recorder.py` — host-mic, single-server state machine
  with VAD-based segmentation. This is the closest thing to "room mode"
  today, but it's bound to *the server's* microphone, not a generic Room.
- `services/live_transcription_service.py` — host-mic continuous
  transcription with a 60-minute circular buffer + retroactive session
  creation.
- `audio_level_direct_usb.py` — direct `arecord` subprocess capture from
  `hw:0,0`. The current "server-attached USB mic" path, but hard-coded.
- `docs/satellite-devices-design.md` — full ESP32 + RPi protocol spec.
  Reusable; this doc references it rather than restating it.
- `docs/WYOMING_PROTOCOL.md` (336 lines) — alternate ESP32 path via
  Home Assistant's Wyoming protocol on port 10700, including wake words.
- `docs/companion-app-design.md` — the Mac/PC companion app spec; relevant
  for room-host machines but a separate user story.
- `docs/always-on-recording-design.md` — silence-gap segmentation spec,
  which this doc reuses for unattended room recording.

### Frontend

- `pages/LiveRecording.tsx` — wraps `AlwaysOnControl`, `DesktopBrowserRecorder`,
  the legacy host-mic UI (hidden behind `false &&`), and the mobile shell.
- `contexts/AlwaysOnContext.tsx` (1542 lines) — VAD, chunk upload, browser
  STT, browser LLM live-summary, device picker, source-mode picker
  (mic/tab/mic+tab).
- `components/AlwaysOnControl.tsx`, `components/DesktopBrowserRecorder.tsx`,
  `components/MobileLiveRecording.tsx` — the three current per-user
  recording surfaces.
- **No frontend code touches `satellite_api`, no `/rooms` page, no
  Conference Room UI exists.** This is the gap.

### Migrations

Through `022_stt_default_parakeet.py` plus the
`b18939734458_drop_orphan_meeting_chunk_embeddings_` revision. Next
migration this design touches: `023_conference_rooms`.

## 3. Mental Model: Three Layers

```
+--------------------------------------------------------------------+
|  Layer 3:  Room                                                    |
|   - Persistent admin-owned entity                                  |
|   - Owns retention policy, ACL, scheduling, naming                 |
|   - Binds to >=1 audio source                                      |
+--------------------------------------------------------------------+
                              |  has many
                              v
+--------------------------------------------------------------------+
|  Layer 2:  Audio Source                                            |
|   - Server-attached mic(s) (ALSA / PipeWire, one or many)          |
|   - Satellite RoomHost (ESP32-S3 / RPi / generic device)           |
|   - Companion app on a room-host machine                           |
|   All paths produce the same chunked audio POSTs the browser does. |
+--------------------------------------------------------------------+
                              |  produces
                              v
+--------------------------------------------------------------------+
|  Layer 1:  Recording Session                                       |
|   - The thing you watch / read / search                            |
|   - source_type tells you which path it came from                  |
|   - room_id tells you which room (NULL for personal/browser)       |
+--------------------------------------------------------------------+
```

The browser/personal path lives entirely in Layer 1 — it has no Layer 2
device and no Layer 3 room. The new mode adds Layer 3 and formalizes the
Layer 2 abstraction.

## 4. User Journey

### 4.1 Initial setup (admin)

1. Admin navigates to `/rooms` in the org dashboard. Empty state with
   "Add Conference Room" CTA.
2. Wizard at `/rooms/new`:
   - Step 1: Name ("Conference Room A") + location ("HQ, 2nd floor").
   - Step 2: Pick an audio source:
     - **Server-attached USB mic** — picker shows the
       `arecord -L` enumeration on the host (PipeWire / ALSA devices).
       Phase 1 PRIMARY pattern; multiple USB mics on the server, each
       bound to its own room, is the default deployment story (see
       Section 5.1).
     - **Satellite RoomHost device** — admin gets a 6-digit pairing
       code; any device (RPi, ESP32, generic Linux box, Windows PC,
       whatever the customer prefers) on the same Tailnet enters it
       during first-boot. The RoomHost POSTs chunks back to the server
       just like a browser user does, with a `room_id` field.
   - Step 3: Recording mode:
     - **Manual only** — admin / member presses Start / Stop in the UI.
     - **Scheduled** — cron-like schedule ("Weekdays 09:00-17:00 ET").
     - **Always-on** — silence-gap segmentation runs 24/7 (reuses
       `services/always_on_recorder.py`).
     - **Wake-word triggered** — Wyoming wake word starts recording.
       Out of scope for Phase 1.
   - Step 4: Retention policy ("90 days" default; admin can pick 7 / 30
     / 90 / 365 / forever / legal-hold).
   - Step 5: ACL ("org members can view recordings from this room" /
     "only these users / groups").
3. After submit: room appears in `/rooms`, status `idle`, hardware
   `healthy` (or `unbound` if no device has paired yet).

### 4.2 Daily use

- **Scheduled** room: cron triggers `room.start_session()` automatically
  at the scheduled time. Session ends on schedule or on silence-gap
  threshold.
- **Always-on** room: device streams continuously; the silence-gap state
  machine in `always_on_recorder.py` opens / closes sessions
  automatically.
- **Manual** room: any org member with `room.record` permission can press
  Start in `/rooms/{id}`. Anyone with `room.record` can press Stop.
- All sessions are visible at `/sessions?room_id={id}` and on the room's
  detail page.

### 4.3 Multi-room

An org can register N rooms. Each room is independent: its own hardware
binding, schedule, retention, ACL. The org's sidebar gets a "Rooms"
section listing all rooms with live status (idle / recording / offline /
error) and a green/red dot.

**Multi-room is native from Day 1.** Rooms work like "many different
browser users submitting concurrently" — N independent capture loops
posting chunks to the same shared `/chunks-text` / `/chunks` endpoints,
each tagged with its own `room_id`. The data model, API surface, and
code paths are all built for N concurrent rooms with no special-case
"single room MVP" mode. The hardware constraint "1 USB mic = 1 room"
is a deployment reality (you physically can't share one mic between
two rooms), not a software limit; the server happily runs N concurrent
`arecord` subprocesses against N distinct USB devices.

## 5. Hardware Sources, Ranked

Ordered simplest -> most flexible. Phase numbers refer to Section 15.

### 5.1 Server-attached USB mic(s) — Phase 1 PRIMARY deployment pattern

The server has one or more USB mics plugged directly into it. Each mic
is dedicated to one room. **This is the Phase 1 primary pattern; the
server-multi-mic story below replaces the original "one room per
server" framing.**

Aaron asked the key question: *can the server have multiple physical
mics connected, each dedicated to a different room, eliminating the
need for satellite hardware in each room?* The answer is **yes,
easily.** Linux ALSA addresses each USB audio device by index. Multiple
concurrent `arecord` processes can each bind to a specific `hw:X,0`. A
USB 3 root hub can host 20+ audio devices without bandwidth issues
(each 16 kHz mono S16_LE stream is ~32 KB/s).

#### Hardware

- USB hub (powered, 7-10 port USB 3) plus N USB mics. Mics can be cheap
  (~$30 omni boundary) or studio-grade (~$200 XLR-to-USB chain with a
  preamp).
- Server runs N concurrent `arecord` processes, each binding to its own
  `hw:X,0`. One subprocess per active room.
- Per-room cost: $200-400 hardware total, no firmware, no WiFi.

#### Wiring

The mic has to physically reach the server. Options by distance:

- **0-5 m** — direct USB cable.
- **5-30 m** — powered USB extender (active repeater).
- **30-100 m** — Cat5/6 USB extender (USB-over-Ethernet, not Ethernet
  networking; uses Cat cable as a passive medium).
- **100 m+** — XLR balanced run to a single XLR-to-USB interface at the
  server. XLR is the right cable for long-haul analog audio.

#### Why this is the right Phase 1 default

Aaron's deployment context is SMB single-office: a server closet plus
4-6 conference rooms within wiring distance. The server-multi-mic
approach has no firmware, no OTA, no WiFi flakiness, and a single point
of management. Pros and cons vs. satellites:

| Dimension | Server-multi-mic (5.1) | Satellite RoomHost (5.3) |
|---|---|---|
| Firmware to maintain | None | Yes (OTA pipeline needed) |
| Single point of management | Yes (one server) | No (per-device updates) |
| Mic quality ceiling | High (XLR-to-USB allowed) | Limited by what fits on the device |
| Single point of failure | Server | Per room (one down doesn't kill others) |
| Relocate a room | Re-run wiring | Move the device |
| Geographically distributed rooms | No (wiring distance) | Yes |
| Per-room cost | $200-400 | $30-300 + ongoing firmware support |

Best fit for 5.1: SMB single-office, server closet + N conference rooms
within wiring distance. **Phase 1 PRIMARY.** Best fit for 5.3:
geographically distributed offices, portable rooms, rooms beyond
wiring distance.

#### Implementation

- Discovery: `arecord -L` enumerates devices on boot. Admin picks one
  in the wizard. We persist `pipewire_node_name` (preferred) or
  `alsa_device_string` (fallback) in `audio_sources.config`.
- Capture: long-lived `arecord -D <device> -f S16_LE -r 16000 -c 1 -t raw`
  subprocess managed by a new `services/room_recorder.py`. **One
  process per active room — N concurrent rooms work natively** because
  each subprocess binds to a different `hw:X,0`.
- Chunking: the subprocess output is VAD-segmented locally and POSTed
  to the existing `/api/recordings/start-always-on/chunks-text` (and
  `/chunks` for the audio bytes) endpoints, the same path the browser
  uses, with an added `room_id` field.
- Failure mode: if a device disappears (USB unplug), that room's state
  goes to `error`; other rooms keep recording. We don't auto-create
  empty sessions during the dead window.

Pros: simplest possible. Zero firmware. No network involved between
mic and server (USB is point-to-point).
Cons: server is single point of failure. Can't relocate rooms without
rewiring. Doesn't help geographically distributed offices.

### 5.2 [reserved]

Originally allocated for a network audio streaming protocol (SRT / RTP
/ Icecast). **Dropped.** Aaron's framing: *"as long [as] we send audio
at the end to the server"* — meaning the existing browser chunk
pattern (VAD + chunk POST via HTTPS) is the universal contract for all
sources. There is no separate streaming protocol. Satellites, host
agents, room-host PCs all POST chunks the same way the browser does.

### 5.3 Satellite RoomHost (ESP32 / RPi / generic device) — Phase 3 scale-out

For rooms beyond wiring distance of the server, geographically
distributed offices, or portable units, a dedicated device per room
acts as a **RoomHost**. The form factor is intentionally generic — we
don't couple to any one piece of hardware:

- RPi (Linux, full Python agent, optional local Whisper).
- ESP32-S3 (microcontroller, lighter firmware).
- Generic Linux box / mini-PC / Intel NUC / Windows PC running a small
  agent.
- Any future device that can hold a Tailscale identity and POST audio
  chunks.

The full hardware-side protocol is already specced in
`satellite-devices-design.md`. **This doc updates that spec on one
critical point**: RoomHosts use the same chunk-POST pattern as the
browser, not a dedicated streaming WebSocket. See Section 6.

Pros: dedicated hardware per room, cheap (ESP32 ~$15, RPi ~$130, mini-PC
~$300), zero-touch on-site once paired, supports store-and-forward for
WiFi-flaky rooms, works across geographically distributed offices,
isolated failure domain per room.
Cons: real hardware fleet to deploy and maintain. Per-device firmware
or agent updates. Pairing UX has to be solid.

### 5.4 Companion app on a room-host Mac / PC [Phase 4]

A persistent room-host machine in the room runs the companion app from
`companion-app-design.md` permanently. The host machine is treated as
a Room device of type `companion-app`, and the agent POSTs chunks the
same way every other source does. This is the high-end / studio-grade
tier for rooms that want system audio capture too (e.g. share a Zoom
mix into the recording). No coupling to any specific hardware brand or
OS — whatever the customer already owns.

## 6. Satellite / RoomHost Protocol

The hardware-side protocol is already specced in
`satellite-devices-design.md`, but this doc **updates that spec on one
critical point**: RoomHosts use the same chunk-POST contract as the
browser, not a dedicated streaming WebSocket. Aaron's framing:
*"as long [as] we send audio at the end to the server"* — there's no
need for a separate streaming protocol, and dropping it removes a large
amount of bespoke code.

### 6.1 Data plane: chunk POST (same as browser)

Every audio source — server-attached mic, RoomHost satellite, companion
app — produces VAD-segmented chunks and POSTs them to the existing
endpoints:

- `POST /api/recordings/start-always-on/chunks-text` — partial / final
  transcript text plus chunk metadata.
- `POST /api/recordings/start-always-on/chunks` — the audio bytes for
  the chunk (WAV or compressed).
- Both requests carry a new `room_id` field (Phase 1) when the source
  is a Conference Room rather than a browser user.
- Auth on the data plane: the **device API key** (`X-API-Key` header)
  for satellites / RoomHosts. For server-attached mics, the
  `room_recorder` service POSTs via internal-loopback HTTPS with an
  internal-service token.

### 6.2 Control plane: heartbeat + lifecycle

- **Auth**: device holds an API key issued at pairing. Stored hashed in
  `satellite_devices.api_key`.
- **Heartbeat**: `POST /api/satellites/{device_id}/heartbeat` every
  30 s with `{ status, battery_pct?, wifi_rssi?, free_sd_mb? }`.
  Server flips device to `offline` after 90 s of silence.
- **Store-and-forward**: device records to SD card if network is down,
  POSTs chunks via the standard chunk endpoint when network returns,
  deletes local file on 200 OK.
- **Local STT (RPi / mini-PC only)**: optional whisper.cpp / faster-whisper
  on the device; transcript chunks ride the same `/chunks-text`
  endpoint with `source='device_stt'`.
- **mDNS discovery**: device looks up `_meetingops-server._tcp.local`;
  server advertises `_meetingops-satellite._tcp.local` for inverse
  discovery. Tailscale MagicDNS works for cross-VLAN.

### 6.3 Legacy streaming WebSocket

The existing `/ws/satellite/{device_id}/audio` WebSocket stays in the
codebase for now — it works, and existing devices in the field may
still be using it — but Phase 3 RoomHost firmware ships against the
chunk POST pattern. We do not invest further in the WS path; new
features land on the chunk endpoints.

### 6.4 Pairing additions on top of existing flow

- **6-digit numeric pairing code**: admin generates from
  `/rooms/{id}/setup`. The code maps to a `room_id`, expires after
  10 minutes, and pre-fills the device's organization + room. Solves
  "who owns this device" cleanly.
- **Room binding**: registration must include a valid pairing code OR
  an existing API key from a previous pairing. Bare registrations
  without a code go to `room_id=NULL` and require admin assignment.
- **Re-pairing**: a device whose API key is revoked can re-pair via a
  new code. Old recordings stay attributed to the same `device_id`.

## 7. Data Model Additions

Migration: `023_conference_rooms`.

### 7.1 New table: `conference_rooms`

```sql
CREATE TABLE conference_rooms (
    id                  SERIAL PRIMARY KEY,
    organization_id     INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name                VARCHAR(200) NOT NULL,
    location            VARCHAR(200),
    description         TEXT,
    -- Recording behavior
    recording_mode      VARCHAR(20) NOT NULL DEFAULT 'manual',
        -- 'manual' | 'scheduled' | 'always_on' | 'wake_word'
    schedule_cron       VARCHAR(100),       -- nullable; e.g. '0 9-17 * * 1-5'
    schedule_timezone   VARCHAR(50),        -- e.g. 'America/New_York'
    silence_gap_minutes INTEGER DEFAULT 5,  -- for always_on mode
    -- Retention
    retention_days      INTEGER,            -- NULL = forever; legal hold sets to NULL too
    legal_hold          BOOLEAN NOT NULL DEFAULT FALSE,
    -- ACL (denormalized for fast filtering; details in room_acl below)
    default_visibility  VARCHAR(20) NOT NULL DEFAULT 'org_members',
        -- 'org_members' | 'restricted' | 'public_link'
    -- Lifecycle
    status              VARCHAR(20) NOT NULL DEFAULT 'idle',
        -- 'idle' | 'recording' | 'offline' | 'error' | 'paused'
    current_session_id  VARCHAR(100),       -- pointer to active recording_sessions.session_id
    last_recording_at   TIMESTAMPTZ,
    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (organization_id, name)
);
CREATE INDEX ix_conference_rooms_org ON conference_rooms (organization_id);
CREATE INDEX ix_conference_rooms_status ON conference_rooms (status);
```

### 7.2 New table: `room_audio_sources`

A room can bind to one *active* source but keep N configured (for
hot-swap during maintenance).

```sql
CREATE TABLE room_audio_sources (
    id              SERIAL PRIMARY KEY,
    room_id         INTEGER NOT NULL REFERENCES conference_rooms(id) ON DELETE CASCADE,
    source_type     VARCHAR(30) NOT NULL,
        -- 'host_mic' | 'satellite' | 'companion_app'
    is_active       BOOLEAN NOT NULL DEFAULT FALSE,
    config          JSONB NOT NULL DEFAULT '{}',
        -- host_mic:        {"pipewire_node": "alsa_input...", "alsa_device": "hw:0,0"}
        -- satellite:       {"device_id": "roomhost-room-a-001"}   -- FK-by-string into satellite_devices
        -- companion_app:   {"device_id": "roomhost-room-a"}
    last_audio_at   TIMESTAMPTZ,
    health_status   VARCHAR(20) DEFAULT 'unknown',
        -- 'healthy' | 'degraded' | 'offline' | 'unbound' | 'error'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_room_audio_sources_room ON room_audio_sources (room_id);
CREATE UNIQUE INDEX ix_room_audio_sources_active
    ON room_audio_sources (room_id) WHERE is_active = TRUE;
```

### 7.3 New table: `room_pairing_codes`

```sql
CREATE TABLE room_pairing_codes (
    id              SERIAL PRIMARY KEY,
    room_id         INTEGER NOT NULL REFERENCES conference_rooms(id) ON DELETE CASCADE,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    code            VARCHAR(8) NOT NULL UNIQUE,   -- 6-digit numeric; UNIQUE globally for cross-org safety
    expires_at      TIMESTAMPTZ NOT NULL,         -- now() + 10 min
    consumed_at     TIMESTAMPTZ,                  -- nullable; set on successful pair
    consumed_by_device_id VARCHAR(100),           -- nullable; the satellite_devices.device_id that consumed it
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX ix_room_pairing_codes_active
    ON room_pairing_codes (code) WHERE consumed_at IS NULL;
```

### 7.4 New table: `room_acl`

Per-room access overrides on top of org membership.

```sql
CREATE TABLE room_acl (
    id              SERIAL PRIMARY KEY,
    room_id         INTEGER NOT NULL REFERENCES conference_rooms(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    email           CITEXT,        -- for external invitees, future
    role            VARCHAR(40) NOT NULL,
        -- free-string. Phase 1 ships with 'admin' | 'member' | 'viewer'.
        -- Schema accepts arbitrary values so that adding e.g.
        -- 'legal_officer', 'auditor', 'compliance' later is a
        -- code-only change with no migration. Permission resolution
        -- is in application code (backend/auth/permissions.py).
    granted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (user_id IS NOT NULL OR email IS NOT NULL)
);
CREATE INDEX ix_room_acl_room_user ON room_acl (room_id, user_id);
CREATE INDEX ix_room_acl_role ON room_acl (room_id, role);
```

Phase 1 ships with `admin`, `member`, `viewer`. Legal hold is
admin-only at launch (Aaron's call). Adding `legal_officer` or any
other role later is a code change in `backend/auth/permissions.py`
without a database migration.

### 7.5 Extend `satellite_devices`

```sql
ALTER TABLE satellite_devices
    ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES conference_rooms(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS pairing_code_id INTEGER REFERENCES room_pairing_codes(id) ON DELETE SET NULL;
-- organization_id is already nullable; backfill from room_id and tighten to NOT NULL once backfilled.
CREATE INDEX ix_satellite_devices_room ON satellite_devices (room_id);
```

### 7.6 Extend `recording_sessions`

```sql
ALTER TABLE recording_sessions
    ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES conference_rooms(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS retention_expires_at TIMESTAMPTZ;  -- null = inherit room policy / forever
CREATE INDEX ix_recording_sessions_room ON recording_sessions (room_id);
CREATE INDEX ix_recording_sessions_retention
    ON recording_sessions (retention_expires_at)
    WHERE retention_expires_at IS NOT NULL AND legal_hold = FALSE;
```

`source_type` already accepts the satellite values. We add one more
constant: `room_host_mic` for Phase 1 server-attached-USB sessions, so
that auditors can tell host-mic sessions apart from the legacy
`local_mic` value used by the silence-gap recorder. We do NOT rename the
existing `local_mic` — that would migration-thrash analytics queries.

**Multi-room concurrent**: the schema above already supports N
concurrent rooms with no special-casing. Every recording_sessions row
carries its own `room_id`; every `room_audio_sources` row scopes
hardware to a specific `room_id`; no shared singleton state. Adding a
second / third / Nth concurrent room is a deployment action (plug in
another USB mic, create another room), not a schema change.

## 8. API Surface

### 8.1 New endpoints — Room CRUD + lifecycle

| Method | Endpoint | Purpose | Auth |
|---|---|---|---|
| `POST` | `/api/rooms` | Admin creates a room | `org_admin` |
| `GET` | `/api/rooms` | List rooms in active org | `org_member` |
| `GET` | `/api/rooms/{id}` | Room detail (live status + sources + recent sessions) | `room.view` |
| `PATCH` | `/api/rooms/{id}` | Update room (name/schedule/retention/ACL) | `org_admin` |
| `DELETE` | `/api/rooms/{id}` | Soft-delete; recordings keep attribution | `org_admin` |
| `POST` | `/api/rooms/{id}/sources` | Bind a new audio source | `org_admin` |
| `PATCH` | `/api/rooms/{id}/sources/{src_id}` | Update source / mark active | `org_admin` |
| `DELETE` | `/api/rooms/{id}/sources/{src_id}` | Remove a source | `org_admin` |
| `POST` | `/api/rooms/{id}/pairing-codes` | Generate a 6-digit pairing code | `org_admin` |
| `GET` | `/api/rooms/{id}/pairing-codes` | List active codes (for debugging) | `org_admin` |
| `POST` | `/api/rooms/{id}/recordings/start` | Manual start | `room.record` |
| `POST` | `/api/rooms/{id}/recordings/stop` | Manual stop | `room.record` |
| `GET` | `/api/rooms/{id}/recordings` | Sessions for this room (filterable) | `room.view` |
| `POST` | `/api/rooms/{id}/probe-source` | Capture 5 s of audio to validate a binding | `org_admin` |
| `GET` | `/api/rooms/host-devices` | Enumerate USB / ALSA / PipeWire devices on the server | `org_admin` |
| `POST` | `/api/rooms/{id}/legal-hold` | Set / clear legal hold (blocks retention purge) | `org_admin` |

### 8.2 Modified endpoints — satellite registration

`POST /api/satellites/register` gains an optional `pairing_code` field.
When supplied:

- Validate code is unused and not expired.
- Set `room_id` and `organization_id` from the code's room.
- Generate API key, return to device.
- Mark code consumed.

When omitted: legacy behavior (creates a device with `room_id=NULL` that
an admin must assign manually via `PATCH /api/satellites/{device_id}`).

### 8.3 Chunk endpoints accept `room_id`

The existing chunk endpoints used by the browser get an additional
optional `room_id` field:

- `POST /api/recordings/start-always-on/chunks-text` — accepts
  `room_id` in the payload. If present, the resulting recording_sessions
  row is created with `room_id` set and `source_type='room_host_mic'`
  (server-attached) or the appropriate satellite source_type.
- `POST /api/recordings/start-always-on/chunks` — same, for the audio
  bytes.

This is the **only** ingestion path for rooms. There is no dedicated
streaming protocol (SRT/RTP/Icecast) and no new "room streaming"
WebSocket. Server-attached mics POST via internal-loopback HTTPS;
satellite RoomHosts POST via Tailscale + their device API key.

### 8.4 Legacy streaming WebSocket — unchanged but no longer the canonical path

`/ws/satellite/{device_id}/audio` stays in the codebase for backward
compatibility with any existing devices. Phase 3 RoomHost firmware
ships against the chunk POST pattern (Section 6.1). New features land
on the chunk endpoints; the WS path is feature-frozen.

### 8.5 Live transcript channel

The room admin UI subscribes to live transcripts via the existing
`/api/live-transcription/ws` channel, scoped by `room_id` (a new query
parameter).

## 9. Frontend Routes & Components

### 9.1 New routes

```
/rooms                          List of rooms, live status grid
/rooms/new                      Setup wizard (Step 1-5)
/rooms/:id                      Detail: live transcript, sources, sessions, settings
/rooms/:id/setup                Re-run the wizard for an existing room
/rooms/:id/pair                 Pairing code display (auto-refreshes every 5 s)
/rooms/:id/sessions             Sessions filtered to this room
```

### 9.2 Renamed / clarified routes

- `/record` -> stays as a redirect alias for backward compatibility, but
  the canonical name becomes **`/record/personal`** to disambiguate from
  `/record/conference/{room_id}` (rare manual-trigger UI).
- Add a redirect: `/record` -> `/record/personal`.
- `LiveRecording.tsx` stays; we add a tiny `<RoomRecordingPanel
  roomId={...} />` for the conference path that wraps the same VAD-free
  transcript stream.

### 9.3 New components

- `pages/Rooms.tsx` — list + live status (red/green/yellow dot, current
  session pill).
- `pages/RoomDetail.tsx` — three tabs: Live (transcript ticker, audio
  level meter, Stop button if `room.record`), Sessions (table filtered
  by `room_id`), Settings (sources, schedule, retention, ACL).
- `pages/RoomSetupWizard.tsx` — 5-step wizard (Section 4.1).
- `components/RoomCard.tsx` — used on `/rooms` list.
- `components/RoomStatusDot.tsx` — pulsing red when recording.
- `components/AudioSourcePicker.tsx` — server-mic dropdown plus
  satellite-RoomHost pairing-code generator. No network-stream URL
  field (dropped from design).
- `components/RoomLiveTranscript.tsx` — WS subscription on
  `/api/live-transcription/ws?room_id={id}`.
- `components/RoomScheduleEditor.tsx` — cron expression builder UI.
- `components/RoomRetentionPanel.tsx` — retention picker + legal-hold
  toggle.
- `services/roomsApi.ts` — typed REST client for `/api/rooms/*`.

The sidebar (`SimplifiedNavigation`) adds a "Rooms" entry between
"Sessions" and "Speakers", visible to all org members (rooms list is
read-visible to all; mutation is admin-only).

## 10. Multi-Mic / Multi-Speaker Handling

Conference rooms often have multiple mics. Three configurations to
support, in order of complexity:

### 10.1 Mic-shared (one channel, everyone on it)

The simplest and most common. Single I2S mic, ceiling mic, or
omnidirectional mic. We diarize with our existing pyannote / ONNX path.
This works today. Phase 1 ships this only.

### 10.2 Mic-per-speaker (N channels, one speaker each)

A boardroom with personal lavaliers or a table-mic array that exposes
per-mic channels. Two sub-cases:

- **Channels arrive at the server already separated** — typically as
  multi-channel WAV chunks POSTed via the standard `/chunks` endpoint,
  one chunk per chunk-window covering all channels. We extend
  `RecordingSession` with a `channels` JSON field that records
  `[{"channel_index": 0, "speaker_id": 17, "label": "Aaron"}]`.
  Diarization becomes trivial — the channel IS the speaker. Each
  channel runs Whisper separately, then we time-merge.
- **Channels need separation server-side** — beamforming, voice
  separation. Out of scope until Phase 4.

### 10.3 Pre-registered speaker roster

Every Conference Room can have a roster of expected attendees
(`room_speaker_roster` table — out of scope for this doc but trivially
addable). Diarization quality is dramatically better with a known-N
prior. Phase 4.

### 10.4 Beamforming / source localization

Out of scope for v1-v3. Flagged for Phase 5 as the "appliance" tier
when we own the hardware.

## 11. STT / LLM Routing for Rooms

Rooms always use **server-side STT**. There is no browser involved when
the source is hardware — pushing a hardware audio stream into WebGPU
makes no sense.

- STT path: same Parakeet-on-midboy2 + Whisper-server-on-bigboy chain
  the rest of the app uses. The router in
  `services/stt_model_manager.py` already picks per-org defaults.
- LLM live-summary path: server-side via `unified_agent_service`. Rooms
  do not need browser-LLM rolling summary; they're not bandwidth-bound.
  Final summary uses the org's configured LLM provider, same as today.
- Privacy mode: **N/A** for rooms (privacy mode is a per-user
  on-device-only flag; rooms are by definition shared / off-device).
  The privacy mode UI should hide itself when viewing room sessions.

## 12. Access Control & Multi-Tenancy

### 12.1 The bug we found in the survey

`api/satellite_api.py` today scopes by `current_user` only. There is no
`organization_id` check on list / get / update / delete. A user in Org A
can see and mutate satellites belonging to Org B. The
`SatelliteDevice.organization_id` column exists but is `nullable=True`
and is NOT filtered in the API queries.

**This must be fixed before Conference Room ships**, otherwise the new
room ACL is meaningless. Ticket included in Section 17.

### 12.2 New permission strings

Permissions are resolved in application code from the free-string
`room_acl.role` value plus the org-level role. Phase 1 ships these
mappings:

| Permission | Granted to | Effect |
|---|---|---|
| `room.view` | `room_acl.role in {'viewer','member','admin'}` OR org member when `default_visibility='org_members'` | Read room + sessions |
| `room.record` | `room_acl.role in {'member','admin'}` OR org `member` role | Manual start / stop |
| `room.manage` | `room_acl.role = 'admin'` OR org `admin` role | Edit settings, ACL, retention |
| `room.legal_hold` | `room_acl.role = 'admin'` OR org `admin` role | Set / clear legal hold (Phase 1) |

ACL resolves in this order: explicit `room_acl` row > `default_visibility` > deny.

Future roles (e.g. `legal_officer`) land as code edits in
`backend/auth/permissions.py` — no migration required because `role`
is a free-string column.

### 12.3 Federation

Cross-UC-instance rooms (e.g. a `magicunicorn.dev` room visible from
`genesisflowlabs.com`) are **out of scope for v1**. The schema is
federation-ready: `room_id` is opaque, devices auth with API keys, all
endpoints accept tokens minted by the Unicorn Commander federation
layer.

## 13. Retention Policy

- Default: 90 days. Configurable per room: 7 / 30 / 90 / 365 / forever
  / legal-hold.
- `retention_expires_at` is set at session-finalize time as
  `session.ended_at + room.retention_days`.
- Legal hold sets `legal_hold=TRUE` on every session and clears
  `retention_expires_at` on those sessions; the room itself flips
  `legal_hold=TRUE` so new sessions inherit.
- A nightly cron (`services/retention_sweeper.py`, new) deletes audio +
  transcripts where `retention_expires_at < now()` AND `legal_hold = FALSE`.
- Soft-delete first (mark `status='archived'`), then hard-delete after
  30 days in archived state. Audit-log every deletion. **Cold-storage
  Garage bucket allocation is punted to Phase 2** (Aaron's call):
  Phase 1 archives stay on the same Garage bucket as live data, just
  flagged `status='archived'`; the dedicated cold-tier bucket lands
  with the retention-enforcement work.
- Compliance hook: webhook `room.retention_expiring_soon` fires 7 days
  before purge so legal can intervene.

## 14. Comparison Matrix: Personal vs Conference Room

| Dimension | Personal / Browser | Conference Room |
|---|---|---|
| Audio source | The user's device mic | Hardware bound to a room |
| Who starts / stops | The user | Schedule / event / authorized user |
| Who can access | Owner + collaborators they invite | Org members (by default) + room ACL |
| Default retention | None enforced (manual delete) | 90 days, per-room override |
| Compute path | Browser STT + browser LLM live-summary; server STT for final | Server STT throughout |
| Recording lifetime | Minutes-hours | Indefinite, often 24/7 |
| Bandwidth / storage | Bounded by user device | Bounded by room schedule + retention |
| Multi-mic | No | Yes (Phase 4+) |
| Privacy mode | Available (on-device-only) | N/A |
| Auth boundary | User JWT | User JWT for control plane + device API key for data plane |
| Legal hold | No | Yes |
| Scheduling | No | Yes (cron) |
| Setup cost | Open `/record` | Admin wizard + optional hardware install |
| Failure mode if server down | User can still record locally (privacy mode) | Satellites store-and-forward; host-mic loses audio |
| Cancellation cost | Close tab | Stop schedule, decommission hardware |

## 15. Phased Rollout

### Phase 1 — Server-multi-mic MVP (target: 1-2 weeks)

Goal: validate end-to-end Conference Room with **server-attached USB
mics, N concurrent rooms natively**.

- Migration `023_conference_rooms` (rooms / sources / acl / pairing
  codes; satellite_devices.room_id; recording_sessions.room_id).
- `api/rooms.py` — CRUD + start/stop + host-device enumeration.
- `services/room_recorder.py` — per-room `arecord` subprocess manager,
  multi-room native (N concurrent processes, each on its own
  `hw:X,0`), VAD locally, chunks POSTed to existing `/chunks-text` and
  `/chunks` endpoints with `room_id`.
- `services/retention_sweeper.py` — basic cron deleter (no archive
  bucket).
- Frontend: `/rooms` list, setup wizard (host-mic step only),
  `/rooms/:id` detail with live transcript + Start / Stop. List + cards
  expect N concurrent rooms; no "MVP single-room" placeholder.
- Bug fix: scope `api/satellite_api.py` by `organization_id`.

What's deliberately not in Phase 1: scheduled mode, satellite RoomHost
devices, legal-hold UX (just the column), multi-mic-per-room. **Multi-
room concurrent IS in Phase 1** (this is the reversal — rooms behave
like browser users, N at once, natively).

### Phase 2 — Scheduled + retention enforcement (target: 1 week)

- Cron-driven start/stop in `room_recorder.py` (APScheduler with
  Postgres job store).
- `RoomScheduleEditor.tsx` UI.
- Cron sweeper (`services/room_scheduler.py`).
- Retention configuration UI + legal-hold UX (column ships in Phase 1).
- Dedicated cold-storage Garage bucket allocation + soft-delete WAV
  move.

### Phase 3 — Satellite RoomHost integration (target: 2-3 weeks)

Reuse the existing device infra, on the **chunk POST pattern**:

- Pairing code system (`room_pairing_codes` table + `/api/rooms/{id}/pairing-codes`).
- Modify `satellite_api.register` to honor codes.
- Frontend: `/rooms/:id/pair` page with auto-refresh code display.
- ESP32 firmware skeleton (`firmware/esp32-satellite/` in the repo) —
  reads pairing code from BLE provisioning or a captive portal, calls
  `/api/satellites/register`, then POSTs chunks to the standard
  `/chunks-text` + `/chunks` endpoints.
- RPi reference image — Ansible playbook in `deploy/rpi-satellite/`
  that installs a Python chunk-POST agent + systemd unit.
- Generic Linux / Windows mini-PC agent — same chunk-POST contract.

### Phase 4 — Multi-mic per room (target: 2-3 weeks)

- Multi-channel WAV ingestion with per-channel speaker labeling.
- Pre-registered speaker roster (`room_speaker_roster`).
- Diarization pipeline branch for "channels are speakers".
- Companion app on a persistent room-host machine (system audio
  capture).

### Phase 5 — Appliance / SDVOSB SMB Product (target: TBD)

- Custom hardware (RPi 5 + beamforming mic array + PoE), pre-imaged.
- Beamforming + source localization (likely Respeaker XVF3800 +
  on-device WebRTC AEC).
- Wake-word activation (reuse Wyoming Protocol path; integrate with
  the existing wake-word doc).
- Federation support so SMBs on different UC tenants can deploy.
- This is the "buy a box, plug it in, recordings appear in your UC"
  product. Phase 5 unlocks the SDVOSB hardware play CJ Williams flagged.

## 16. Decisions (resolved with Aaron)

The eight open questions are resolved. Decisions captured here are the
canonical source for downstream implementation.

1. **Phase 1 single-room limit** — REJECTED. Rooms function like "many
   different browser users submitting concurrently." Multi-room
   concurrent is the natural design from Day 1: N independent
   `arecord` subprocesses, N rows in `room_audio_sources`, N
   `recording_sessions` rows with `room_id`, all riding the same
   `/chunks-text` + `/chunks` endpoints. The hardware constraint "1
   USB mic = 1 room at a time" is a deployment reality, not a software
   limit. Sections 4.3 + 5.1 + 7 + 15 updated.

2. **Storage tiering** — ACCEPTED. Cold-storage Garage bucket
   allocation punted to Phase 2 when retention is actually enforced.
   Phase 1 archives stay on the live bucket flagged
   `status='archived'`. Section 13 + 15 updated.

3. **Mac Mini as room host** — REJECTED. No Mac Mini coupling.
   Generic `RoomHost` abstraction: any device (RPi, ESP32, generic
   Linux, Windows PC, mini-PC) can be a RoomHost. Sections 1 + 3 + 4.1
   + 5.3 + 5.4 updated.

4. **Network audio streaming protocol (SRT / RTP / Icecast)** —
   REJECTED ENTIRELY. Aaron's framing: *"as long [as] we send audio
   at the end to the server"* — the existing browser chunk pattern
   (VAD + chunk POST via HTTPS) is the universal contract. No SRT, no
   RTP, no Icecast. Rooms use the SAME `/chunks-text` + `/chunks`
   endpoints as browser users, just with a `room_id` field. Massive
   simplification. Sections 5 + 6 + 8 updated.

5. **6-digit numeric pairing codes** — ACCEPTED. 1M code space,
   10-min TTL, rate-limited. Implementation already in 7.3.

6. **APScheduler with Postgres job store** — ACCEPTED, with proviso
   "as long as it doesn't mess anything up or cost us anything from a
   capability or functionality wise." APScheduler is the in-process
   FastAPI-standard scheduler; the Postgres job store survives
   restarts (jobs persist), there's no capability loss vs. a separate
   systemd timer, and we keep job ownership inside the same auth /
   tenant boundary as the rest of the app. Sections 13 + 17 updated.

7. **Admins-only legal hold, role schema flexible** — ACCEPTED with
   "whatever's best for the long term." Phase 1: only `admin` role
   can set / clear legal hold. The `room_acl.role` column is a free
   string (Section 7.4), so adding `legal_officer`, `auditor`, etc.
   later is a code-only change in `backend/auth/permissions.py` with
   no migration. Sections 7.4 + 12.2 updated.

8. **Beamforming hardware** — DEFERRED. Phase 5 partner decision
   (Respeaker XVF3800 vs. custom MEMS array). No change.

### Server-multi-mic clarification (Aaron's question that re-shaped Phase 1)

Aaron asked: *can the server have multiple physical mics connected,
each dedicated to a different room, eliminating the need for satellite
hardware in each room?* The answer is yes, easily, and it improves the
Phase 1 deployment story significantly. Linux ALSA addresses each USB
audio device by index; N concurrent `arecord` processes each bind to
a specific `hw:X,0`; USB 3 hosts 20+ audio devices per root hub
without bandwidth issues (each 16 kHz mono stream is ~32 KB/s). This
became the Phase 1 PRIMARY pattern; satellite RoomHosts (Section 5.3)
are now Phase 3 scale-out for rooms beyond wiring distance,
geographically distributed offices, or portable units. They serve
different deployment contexts. Section 5.1 expanded.

## 17. Ticket Breakdown

Each ticket is sized S (~1 day), M (~2-4 days), L (1-2 weeks). All file
paths are absolute under `/srv/meeting-ops/src/`.

### CR-001 [S] Fix multi-tenancy bug in satellite_api.py
- **Scope**: scope every query in `backend/api/satellite_api.py` by
  `organization_id`; require `get_current_organization` dep alongside
  `get_current_user`; backfill `satellite_devices.organization_id`
  from the inviter; flip column to `NOT NULL`.
- **Deps**: none. Blocker for CR-005 onward.
- **Files**: `backend/api/satellite_api.py`,
  `backend/alembic/versions/022_*.py` (new migration to backfill +
  tighten).

### CR-002 [M] Migration `023_conference_rooms`
- **Scope**: create `conference_rooms`, `room_audio_sources`,
  `room_pairing_codes`, `room_acl`; extend `satellite_devices` +
  `recording_sessions` per Section 7.
- **Deps**: CR-001.
- **Files**: `backend/alembic/versions/023_conference_rooms.py`,
  `backend/database/models.py`.

### CR-003 [L] Backend: `api/rooms.py` (CRUD + lifecycle)
- **Scope**: all endpoints in Section 8.1 (room CRUD, source binding,
  pairing-code generation, manual start / stop, recordings filter,
  host-device enumeration, source probe). Pydantic models + permission
  checks against `room_acl`.
- **Deps**: CR-002.
- **Files**: `backend/api/rooms.py` (new), `backend/main.py`
  (router registration), `backend/auth/permissions.py` (new
  permission strings).

### CR-004 [M] Backend: `services/room_recorder.py`
- **Scope**: per-room `arecord` subprocess manager, **multi-room
  native** from Day 1 (N concurrent rooms = N concurrent processes,
  each bound to its own `hw:X,0`). VAD applied locally per subprocess;
  chunk POSTs to the existing `/api/recordings/start-always-on/chunks-text`
  and `/chunks` endpoints with a `room_id` field via internal-loopback
  HTTPS (internal-service token). No dedicated streaming protocol.
  Handles process restart on device-disappear with backoff, per room.
- **Deps**: CR-002.
- **Files**: `backend/services/room_recorder.py` (new),
  `backend/services/working_audio_service.py` (extract reusable
  host-device enumeration), small refactor of
  `audio_level_direct_usb.py`.

### CR-005 [M] Pairing code system
- **Scope**: code generation endpoint, validation in
  `satellite_api.register`, expiry / consumption logic, frontend
  `/rooms/:id/pair` page.
- **Deps**: CR-001, CR-002, CR-003.
- **Files**: `backend/api/rooms.py` (`/pairing-codes` endpoints),
  `backend/api/satellite_api.py` (consume codes in register),
  `frontend/src/pages/RoomPair.tsx` (new).

### CR-006 [L] Frontend: `/rooms` + setup wizard
- **Scope**: list page, detail page (Live / Sessions / Settings tabs),
  5-step wizard, audio-source picker, sidebar entry, redirect
  `/record` -> `/record/personal`, room status dot, room card.
- **Deps**: CR-003.
- **Files**: `frontend/src/pages/Rooms.tsx`,
  `frontend/src/pages/RoomDetail.tsx`,
  `frontend/src/pages/RoomSetupWizard.tsx`,
  `frontend/src/components/RoomCard.tsx`,
  `frontend/src/components/RoomStatusDot.tsx`,
  `frontend/src/components/AudioSourcePicker.tsx`,
  `frontend/src/components/RoomLiveTranscript.tsx`,
  `frontend/src/services/roomsApi.ts`,
  `frontend/src/AppRouterSimplified.tsx`.

### CR-007 [M] Live transcript WS room scoping
- **Scope**: add `?room_id={id}` query parameter to
  `/api/live-transcription/ws`; broadcast room-tagged segments;
  frontend subscribes per-room on `RoomDetail`.
- **Deps**: CR-003, CR-004.
- **Files**: `backend/api/live_transcription.py`,
  `backend/services/live_transcription_service.py`,
  `frontend/src/components/RoomLiveTranscript.tsx`.

### CR-008 [M] Retention sweeper
- **Scope**: nightly job that purges sessions where
  `retention_expires_at < now()` AND `legal_hold = FALSE`. Two-stage:
  soft-delete (status='archived') first, hard-delete after 30 days.
  Audit log on every deletion. APScheduler with Postgres job store.
- **Deps**: CR-002.
- **Files**: `backend/services/retention_sweeper.py` (new),
  `backend/services/scheduler.py` (new APScheduler bootstrap),
  `backend/main.py` (start scheduler).

### CR-009 [M] Scheduled-mode recording
- **Scope**: cron expression on `conference_rooms.schedule_cron`;
  APScheduler triggers start / stop. Time-zone-aware. UI:
  `RoomScheduleEditor.tsx` with cron presets ("Weekdays 9-5", "Custom...").
- **Deps**: CR-003, CR-004, CR-008 (shares APScheduler).
- **Files**: `backend/services/room_scheduler.py` (new),
  `backend/api/rooms.py` (cron validation),
  `frontend/src/components/RoomScheduleEditor.tsx`.

### CR-010 [M] Always-on mode wiring
- **Scope**: connect `services/always_on_recorder.py` to a Room.
  Today it's a singleton; refactor to per-room instances keyed by
  `room_id`. Silence-gap threshold from `conference_rooms.silence_gap_minutes`.
- **Deps**: CR-002, CR-004.
- **Files**: `backend/services/always_on_recorder.py`,
  `backend/api/rooms.py` (mode='always_on' handler).

### CR-011 [L] Satellite device frontend
- **Scope**: surface paired devices in `/rooms/:id` detail (Sources
  tab), show device telemetry (battery, RSSI, free SD), allow
  re-pair / decommission. Hook up the existing `satellite_api.py`
  endpoints (now org-scoped after CR-001).
- **Deps**: CR-001, CR-005, CR-006.
- **Files**: `frontend/src/components/SatelliteDevicePanel.tsx` (new),
  `frontend/src/services/satellitesApi.ts` (new),
  `frontend/src/pages/RoomDetail.tsx`.

### CR-012 [L] ESP32 RoomHost firmware skeleton
- **Scope**: minimal Arduino / ESP-IDF project that pairs via captive
  portal, then POSTs chunked I2S audio to the existing
  `/api/recordings/start-always-on/chunks-text` + `/chunks` endpoints
  with `room_id`. mDNS server discovery. Heartbeat every 30 s. Status
  LED. No SD card / store-and-forward in this ticket (separate ticket
  later). **No streaming WS path** — chunk POST only.
- **Deps**: CR-005.
- **Files**: `firmware/esp32-satellite/` (new repo subdir),
  `firmware/esp32-satellite/README.md` (assembly + flash steps).

### CR-013 [L] RPi RoomHost reference image
- **Scope**: Ansible playbook that installs a Python chunk-POST agent
  + systemd unit. Pairs via QR code shown on first boot (HDMI). Agent
  POSTs chunks to the existing `/chunks-text` + `/chunks` endpoints,
  same path as the browser, with the device API key. Optional local
  Whisper via `whisper.cpp` writing to `/chunks-text` with
  `source='device_stt'`.
- **Deps**: CR-005.
- **Files**: `deploy/rpi-satellite/playbook.yml` (new),
  `deploy/rpi-satellite/agent.py` (new),
  `deploy/rpi-satellite/README.md`.

### CR-014 [M] Legal hold + audit log
- **Scope**: `/api/rooms/{id}/legal-hold` endpoint; admin UI to set
  / clear; audit-log table entry on every change; retention sweeper
  skips legal-hold sessions; UI shows a lock badge on held sessions.
  Phase 1 gates the endpoint on `role='admin'`; future
  `legal_officer` role lands as a code change in
  `backend/auth/permissions.py` (no migration needed).
- **Deps**: CR-002, CR-008.
- **Files**: `backend/api/rooms.py`, `backend/services/retention_sweeper.py`,
  `frontend/src/components/RoomRetentionPanel.tsx`.

### CR-015 [S] Documentation + sample configs
- **Scope**: update `README.md` + `INSTALL.md` with a Conference Room
  section emphasizing the server-multi-mic Phase 1 pattern; ship a
  sample config (`rooms/example-host-mic.json`); add to the CHANGELOG.
- **Deps**: CR-006 (so the UI is real).
- **Files**: `README.md`, `INSTALL.md`, `docs/rooms-quickstart.md`
  (new).

## 18. Out of Scope (Explicit)

- Real-time multi-room audio mixing (e.g. "Room A and Room B as a single
  session"). Sessions are per-room.
- Cross-tenant federation of rooms (covered in `FEDERATION_TRUST_MODES.md`
  separately).
- Live broadcast of room audio to external clients. Rooms are recording
  surfaces, not streaming surfaces.
- AEC / echo cancellation for satellites — defer to hardware that
  ships with on-device AEC (Respeaker, MEMS arrays with DSPs).
- Per-channel speaker re-identification across sessions (Phase 4+
  enhancement on top of the speaker library).
- Mobile satellite clients (e.g. a phone in a coat pocket). Use the
  existing personal/browser path.

## 19. Compatibility Notes

- The new mode adds tables and columns. **No existing data is
  modified.** All current `recording_sessions` rows keep `room_id =
  NULL` and are classified as personal sessions.
- `source_type` accepts a new value (`room_host_mic`) but the existing
  values are unchanged.
- All new API endpoints are namespaced under `/api/rooms/*`. The
  existing `/api/satellites/*` endpoints continue to work; the only
  observable change is that registration starts honoring a new optional
  `pairing_code` field and queries now filter by org.
- Frontend: `/record` keeps working (redirects to `/record/personal`).
  No existing user-facing URL breaks.

## 20. Success Criteria

Phase 1 is "done" when an admin can:

1. Plug **two or more** USB mics into bigboy via a USB hub.
2. Open `/rooms`, click "Add Conference Room" twice, pick a different
   USB mic in each wizard run.
3. Click Start on both rooms. Both room status dots flip to red /
   "Recording" simultaneously. Each room's live transcript appears on
   its own detail page, independently.
4. Click Stop on each. Two new sessions appear in `/sessions`, each
   with the correct `room_id`, WAV on disk, summary generated.
5. Open each session, listen back, read the transcript. Looks like any
   other session except tagged with the appropriate room.

If multi-room concurrent works end-to-end without touching the
existing browser-recording flow, Phase 1 is shipped.
