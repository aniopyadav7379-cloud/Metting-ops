// Conference Room API client.
//
// Wraps every /api/rooms endpoint with a single auth+org-header build.
//
// Mock fallback (CR-006): when the backend hadn't shipped the endpoints
// yet, this module flipped to an in-memory MOCK_STORE on any 404. The
// mocks include two sample rooms ("Conference Room A (sample)" etc.)
// which is *fine* in early dev but a real liability in production —
// users can see fake rooms if a deploy regression 404s a real endpoint.
//
// As of v3.19 the backend rooms API is shipped, so the mock path is now
// off by default and only re-enabled when the `VITE_USE_ROOM_MOCKS`
// build-time flag is set to a truthy string. Production builds should
// never set it. See the audit note "roomsApi.ts:1, 488" for context.

import { config } from '../config';

const MOCKS_ENABLED = (() => {
  try {
    const flag = (import.meta as any)?.env?.VITE_USE_ROOM_MOCKS;
    if (typeof flag === 'string') return flag === '1' || flag.toLowerCase() === 'true';
    return !!flag;
  } catch {
    return false;
  }
})();

export type RoomStatus = 'idle' | 'recording' | 'offline' | 'error' | 'paused';
export type RoomSourceType =
  | 'server_usb_mic'
  | 'network_stream'
  | 'satellite'
  | 'satellite_device'
  | 'companion_app';
export type RoomSourceStatus = 'idle' | 'recording' | 'error' | 'disabled';
export type RoomRole = 'view' | 'record' | 'manage';

/**
 * Frontend Room shape. Note: the backend returns `id` as a string UUID
 * and uses `default_retention_days` instead of `retention_days`. We
 * normalize to numeric id (parsed) and retention_days at the boundary
 * — see `normalizeRoom()`.
 */
export interface Room {
  id: number;
  organization_id: number;
  name: string;
  location?: string | null;
  description?: string | null;
  status: RoomStatus;
  recording_mode: 'manual' | 'scheduled' | 'always_on' | 'wake_word';
  retention_days: number | null;
  legal_hold: boolean;
  current_session_id?: string | null;
  current_session_started_at?: string | null;
  last_recording_at?: string | null;
  created_at: string;
  updated_at: string;
  sources?: RoomSource[];
  /** Stash the raw upstream id so URL params keep working when the
   * backend issues UUIDs that don't parse to a finite Number. */
  raw_id?: string;
}

export interface RoomSource {
  id: number;
  room_id: number;
  hardware_type: RoomSourceType;
  device_path?: string | null;
  device_id?: string | null;
  label: string;
  status: RoomSourceStatus;
  is_active: boolean;
  health_status: 'healthy' | 'degraded' | 'offline' | 'unbound' | 'error' | 'unknown';
  config_json?: Record<string, unknown> | null;
  created_at: string;
  raw_id?: string;
}

/**
 * Map the backend's string ids and slightly different field names to the
 * frontend shape used across the rooms UI. If the input is already in
 * frontend shape (e.g. from mocks) the cast is a no-op.
 */
function normalizeRoom(raw: unknown): Room {
  const r = raw as Record<string, unknown>;
  const idRaw = r.id;
  const id =
    typeof idRaw === 'number'
      ? idRaw
      : typeof idRaw === 'string' && /^\d+$/.test(idRaw)
      ? Number(idRaw)
      : // fallback: hash the UUID into a stable positive int. Used only
        // for `key=` props; URL routing uses raw_id when set.
        Math.abs(
          Array.from(String(idRaw)).reduce(
            (acc, ch) => (acc * 31 + ch.charCodeAt(0)) | 0,
            7,
          ),
        ) || 1;
  const status = (r.status as RoomStatus) || 'idle';
  const retention =
    typeof r.retention_days === 'number'
      ? (r.retention_days as number)
      : typeof r.default_retention_days === 'number'
      ? (r.default_retention_days as number)
      : null;

  // recording_mode isn't in the backend's RoomResponse yet — derive
  // from schedule_json when present, fall back to 'manual'.
  const sched = (r.schedule_json as Record<string, unknown> | null) || null;
  const recording_mode = (sched?.mode as Room['recording_mode']) || 'manual';

  return {
    id,
    raw_id: typeof idRaw === 'string' ? idRaw : undefined,
    organization_id: Number(r.organization_id) || 0,
    name: String(r.name || ''),
    location: (r.location as string | null) || null,
    description: (r.description as string | null) || null,
    status,
    recording_mode,
    retention_days: retention,
    legal_hold: !!r.legal_hold,
    current_session_id: (r.current_session_id as string | null) || null,
    current_session_started_at: (r.current_session_started_at as string | null) || null,
    last_recording_at: (r.last_recording_at as string | null) || null,
    created_at: String(r.created_at || ''),
    updated_at: String(r.updated_at || ''),
    sources: Array.isArray(r.sources) ? (r.sources as unknown[]).map(normalizeSource) : undefined,
  };
}

function normalizeSource(raw: unknown): RoomSource {
  const s = raw as Record<string, unknown>;
  const idRaw = s.id;
  const id =
    typeof idRaw === 'number'
      ? idRaw
      : typeof idRaw === 'string' && /^\d+$/.test(idRaw)
      ? Number(idRaw)
      : Math.abs(
          Array.from(String(idRaw)).reduce(
            (acc, ch) => (acc * 31 + ch.charCodeAt(0)) | 0,
            7,
          ),
        ) || 1;
  const roomIdRaw = s.room_id;
  const room_id =
    typeof roomIdRaw === 'number'
      ? roomIdRaw
      : typeof roomIdRaw === 'string' && /^\d+$/.test(roomIdRaw)
      ? Number(roomIdRaw)
      : 0;
  const status =
    s.status === 'recording' ||
    s.status === 'error' ||
    s.status === 'disabled' ||
    s.status === 'idle'
      ? (s.status as RoomSourceStatus)
      : 'idle';
  const isActive =
    typeof s.is_active === 'boolean'
      ? (s.is_active as boolean)
      : status !== 'disabled';
  const healthMap: Record<string, RoomSource['health_status']> = {
    idle: 'healthy',
    recording: 'healthy',
    disabled: 'offline',
    error: 'error',
  };
  const health_status = (healthMap[status] as RoomSource['health_status']) || 'unknown';

  return {
    id,
    raw_id: typeof idRaw === 'string' ? idRaw : undefined,
    room_id,
    hardware_type: (s.hardware_type as RoomSourceType) || 'server_usb_mic',
    device_path: (s.device_path as string | null) || null,
    device_id: typeof s.device_id === 'string' ? s.device_id : null,
    label:
      String(s.label || '') ||
      String(s.device_path || '') ||
      String(s.hardware_type || ''),
    status,
    is_active: isActive,
    health_status,
    config_json:
      s.config_json && typeof s.config_json === 'object'
        ? (s.config_json as Record<string, unknown>)
        : null,
    created_at: String(s.created_at || ''),
  };
}

export interface PairingCode {
  code: string;
  expires_at: string;
  room_id: number;
}

/**
 * Result of redeeming a pairing code with a device_id. The
 * ``device_secret`` field is the PLAINTEXT secret returned exactly once
 * — devices must persist it locally; it cannot be re-fetched. Lose it ⇒
 * re-pair.
 *
 * The legacy redemption path (no device_id supplied) returns the same
 * shape with the device_* fields all null.
 */
export interface PairingRedeemResult {
  room_id: string;
  redeemed_at: string;
  device_id?: string | null;
  organization_id?: number | null;
  device_secret?: string | null;
  secret_warning?: string | null;
}

export interface RoomAclEntry {
  user_id: number;
  username?: string | null;
  email?: string | null;
  full_name?: string | null;
  role: RoomRole;
  granted_at?: string | null;
}

export interface OrgUser {
  id: number;
  username: string;
  email?: string | null;
  full_name?: string | null;
  role?: string | null;
}

export interface AudioDevice {
  device_path: string;
  card_name: string;
  device_name: string;
}

/** One-shot mic-level probe result. dB values are dBFS (0 = clip, -90 floor). */
export interface AudioDeviceProbe {
  device_path: string;
  duration_sec: number;
  sample_count: number;
  rms_db: number;
  peak_db: number;
  detected_audio: boolean;
}

/** Live audio level frame from the SSE /levels stream. */
export interface RoomLevelFrame {
  rms_db: number;
  peak_db: number;
  timestamp: string;
  active: boolean;
}

/** Reply from `GET /api/rooms/{id}/active-session`. */
export interface RoomActiveSession {
  room_id: string;
  session_id: string | null;
  session_pk: number | null;
  started_at: string | null;
  status: string | null;
}

/**
 * Mid-recording transcript poll result. Built from the simple-recording-db
 * GET session endpoint (`/api/recording-sessions/{id}`). Only the fields
 * we need on the Live tab are typed.
 */
export interface LiveSessionPoll {
  id: string;
  session_id: string;
  title: string;
  status: string;
  duration: number;
  transcript_simple: string;
  transcript_diarized?: {
    segments?: Array<{
      text: string;
      speaker?: string | null;
      start?: number;
      end?: number;
      confidence?: number;
    }>;
  } | null;
  transcription_segments?: Array<{
    text: string;
    speaker?: string | null;
    start?: number;
    end?: number;
    confidence?: number;
  }>;
  progressive_summaries?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  /** Non-null when the session is a conference-room recording. Drives
   * the server-rolled vs client-rolled live-summary path. */
  room_id?: string | null;
  room_source_id?: string | null;
  room_name?: string | null;
}

/** Server-rolled live summary slice for a recording session.
 *
 * For browser always-on sessions the slice list is still client state and
 * does not flow through these endpoints. For room sessions (room_id set)
 * the slices are persisted in `processing_metadata.summary_slices` and
 * returned by `GET /api/recordings/sessions/{id}/summary-slices`. */
export interface SummarySlice {
  id: string;
  text: string;
  word_count: number;
  word_range_start: number;
  word_range_end: number;
  model?: string | null;
  provider?: string | null;
  created_at?: string | null;
  triggered_by: string;
}

export interface SummarySlicesResponse {
  session_id: string;
  is_room_session: boolean;
  slices: SummarySlice[];
  trigger_words: number;
  max_per_session: number;
}

export interface CreateRoomBody {
  name: string;
  location?: string | null;
  default_retention_days?: number | null;
  /**
   * Persisted on conference_rooms.schedule_json (JSONB). Used to round-trip
   * the wizard's recording mode (`mode`) + visibility (`access`) selections
   * — see `RoomSetupWizard.handleSubmit`. `normalizeRoom` reads `mode`
   * back as the Room's recording_mode.
   */
  schedule_json?: Record<string, unknown> | null;
}

export interface UpdateRoomBody {
  name?: string;
  location?: string | null;
  retention_days?: number | null;
  legal_hold?: boolean;
}

export interface AddSourceBody {
  hardware_type: RoomSourceType;
  device_path?: string | null;
  device_id?: string | null;
  label: string;
}

export interface RecordingSessionSummary {
  id: string;
  title?: string | null;
  created_at: string | null;
  duration?: number | null;
  status: string;
  summary_preview?: string | null;
}

// --- Mock fallback layer ---------------------------------------------------

let MOCK_MODE = false;

interface MockStore {
  rooms: Room[];
  sources: RoomSource[];
  acl: Map<number, RoomAclEntry[]>;
  pairing: Map<number, PairingCode>;
  nextRoomId: number;
  nextSourceId: number;
}

// Seed sample rooms are only useful when the dev wants to exercise the
// rooms UI without a backend. We start the store empty unless mocks
// were explicitly enabled at build time.
const MOCK_STORE: MockStore = MOCKS_ENABLED
  ? {
  rooms: [
    {
      id: 1,
      organization_id: 1,
      name: 'Conference Room A (sample)',
      location: 'HQ, 2nd floor',
      description: null,
      status: 'recording',
      recording_mode: 'manual',
      retention_days: 90,
      legal_hold: false,
      current_session_id: 'mock-session-1',
      current_session_started_at: new Date(Date.now() - 42 * 60 * 1000).toISOString(),
      last_recording_at: new Date(Date.now() - 42 * 60 * 1000).toISOString(),
      created_at: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
      updated_at: new Date().toISOString(),
    },
    {
      id: 2,
      organization_id: 1,
      name: 'Conference Room B (sample)',
      location: 'Building B, Floor 3',
      description: null,
      status: 'idle',
      recording_mode: 'manual',
      retention_days: 90,
      legal_hold: false,
      current_session_id: null,
      current_session_started_at: null,
      last_recording_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
      created_at: new Date(Date.now() - 12 * 24 * 60 * 60 * 1000).toISOString(),
      updated_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString(),
    },
  ],
  sources: [
    {
      id: 1,
      room_id: 1,
      hardware_type: 'server_usb_mic',
      device_path: 'hw:2,0',
      device_id: null,
      label: 'USB mic: hw:2,0',
      status: 'recording',
      is_active: true,
      health_status: 'healthy',
      config_json: null,
      created_at: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
    },
    {
      id: 2,
      room_id: 1,
      hardware_type: 'server_usb_mic',
      device_path: 'hw:3,0',
      device_id: null,
      label: 'Ceiling mic',
      status: 'idle',
      is_active: true,
      health_status: 'healthy',
      config_json: null,
      created_at: new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString(),
    },
    {
      id: 3,
      room_id: 2,
      hardware_type: 'server_usb_mic',
      device_path: 'hw:3,0',
      device_id: null,
      label: 'USB mic: hw:3,0',
      status: 'recording',
      is_active: true,
      health_status: 'healthy',
      config_json: null,
      created_at: new Date(Date.now() - 12 * 24 * 60 * 60 * 1000).toISOString(),
    },
  ],
  acl: new Map(),
  pairing: new Map(),
  nextRoomId: 3,
  nextSourceId: 4,
}
  : {
      rooms: [],
      sources: [],
      acl: new Map(),
      pairing: new Map(),
      nextRoomId: 1,
      nextSourceId: 1,
    };

function pseudoSleep(): Promise<void> {
  return new Promise((res) => setTimeout(res, 80));
}

function generatePairingCode(): string {
  // 6-digit numeric, no leading zero stripping.
  const n = Math.floor(100000 + Math.random() * 900000);
  return String(n);
}

// --- HTTP layer ------------------------------------------------------------

interface RoomsApiOptions {
  orgSlug: string | null;
}

function buildHeaders(orgSlug: string | null): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  try {
    const token = localStorage.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
  } catch {
    /* ignore storage failures */
  }
  if (orgSlug) headers['X-MeetingOps-Org'] = orgSlug;
  return headers;
}

async function request<T>(
  url: string,
  options: RequestInit & { orgSlug: string | null },
): Promise<T> {
  const { orgSlug, ...init } = options;
  const headers = {
    ...buildHeaders(orgSlug),
    ...(init.headers as Record<string, string> | undefined),
  };
  const res = await fetch(`${config.apiBaseUrl}${url}`, {
    ...init,
    headers,
    credentials: 'include',
  });
  if (res.status === 404) {
    // Pre-v3.19 the mock fallback flipped on the first 404, but that
    // meant a regressed real endpoint silently showed sample rooms in
    // production. Now we only flip when `VITE_USE_ROOM_MOCKS` is set;
    // otherwise the 404 surfaces to the caller as a real error.
    if (MOCKS_ENABLED) {
      MOCK_MODE = true;
      throw new MockFallbackNeeded(url);
    }
  }
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body);
    } catch {
      detail = `${res.status} ${res.statusText}`;
    }
    throw new Error(detail || `Request failed: ${url}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

class MockFallbackNeeded extends Error {
  constructor(url: string) {
    super(`Mock fallback for ${url}`);
    this.name = 'MockFallbackNeeded';
  }
}

function isMockFallback(err: unknown): err is MockFallbackNeeded {
  return err instanceof MockFallbackNeeded || (err as Error)?.name === 'MockFallbackNeeded';
}

// --- Public API ------------------------------------------------------------

export function isMockMode(): boolean {
  return MOCK_MODE;
}

export const roomsApi = {
  async list({ orgSlug }: RoomsApiOptions): Promise<Room[]> {
    try {
      const raw = await request<unknown[]>('/api/rooms', { orgSlug, method: 'GET' });
      return raw.map(normalizeRoom);
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      return MOCK_STORE.rooms.map((r) => ({
        ...r,
        sources: MOCK_STORE.sources.filter((s) => s.room_id === r.id),
      }));
    }
  },

  async get(id: number | string, { orgSlug }: RoomsApiOptions): Promise<Room> {
    try {
      const raw = await request<unknown>(`/api/rooms/${id}`, { orgSlug, method: 'GET' });
      return normalizeRoom(raw);
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const r = MOCK_STORE.rooms.find((x) => x.id === Number(id));
      if (!r) throw new Error('Room not found');
      return { ...r, sources: MOCK_STORE.sources.filter((s) => s.room_id === Number(id)) };
    }
  },

  async create(body: CreateRoomBody, { orgSlug }: RoomsApiOptions): Promise<Room> {
    try {
      const raw = await request<unknown>('/api/rooms', {
        orgSlug,
        method: 'POST',
        body: JSON.stringify(body),
      });
      return normalizeRoom(raw);
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const now = new Date().toISOString();
      const newRoom: Room = {
        id: MOCK_STORE.nextRoomId++,
        organization_id: 1,
        name: body.name,
        location: body.location || null,
        description: null,
        status: 'idle',
        recording_mode: 'manual',
        retention_days: body.default_retention_days ?? 90,
        legal_hold: false,
        current_session_id: null,
        last_recording_at: null,
        created_at: now,
        updated_at: now,
      };
      MOCK_STORE.rooms.push(newRoom);
      return { ...newRoom, sources: [] };
    }
  },

  async update(id: number | string, body: UpdateRoomBody, { orgSlug }: RoomsApiOptions): Promise<Room> {
    // Backend uses `default_retention_days`; map our `retention_days`
    // through so it lands correctly.
    const wireBody: Record<string, unknown> = { ...body };
    if ('retention_days' in body) {
      wireBody.default_retention_days = body.retention_days;
      delete wireBody.retention_days;
    }
    try {
      const raw = await request<unknown>(`/api/rooms/${id}`, {
        orgSlug,
        method: 'PUT',
        body: JSON.stringify(wireBody),
      });
      return normalizeRoom(raw);
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const idx = MOCK_STORE.rooms.findIndex((x) => x.id === Number(id));
      if (idx === -1) throw new Error('Room not found');
      MOCK_STORE.rooms[idx] = {
        ...MOCK_STORE.rooms[idx],
        ...body,
        updated_at: new Date().toISOString(),
      } as Room;
      return {
        ...MOCK_STORE.rooms[idx],
        sources: MOCK_STORE.sources.filter((s) => s.room_id === Number(id)),
      };
    }
  },

  async remove(id: number | string, { orgSlug }: RoomsApiOptions): Promise<void> {
    try {
      await request<void>(`/api/rooms/${id}`, { orgSlug, method: 'DELETE' });
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      MOCK_STORE.rooms = MOCK_STORE.rooms.filter((r) => r.id !== Number(id));
      MOCK_STORE.sources = MOCK_STORE.sources.filter((s) => s.room_id !== Number(id));
      MOCK_STORE.acl.delete(Number(id));
      MOCK_STORE.pairing.delete(Number(id));
    }
  },

  async listSources(roomId: number | string, { orgSlug }: RoomsApiOptions): Promise<RoomSource[]> {
    try {
      const raw = await request<unknown[]>(`/api/rooms/${roomId}/sources`, {
        orgSlug,
        method: 'GET',
      });
      return raw.map(normalizeSource);
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      return MOCK_STORE.sources.filter((s) => s.room_id === Number(roomId));
    }
  },

  async addSource(
    roomId: number | string,
    body: AddSourceBody,
    { orgSlug }: RoomsApiOptions,
  ): Promise<RoomSource> {
    try {
      const raw = await request<unknown>(`/api/rooms/${roomId}/sources`, {
        orgSlug,
        method: 'POST',
        body: JSON.stringify(body),
      });
      return normalizeSource(raw);
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const newSrc: RoomSource = {
        id: MOCK_STORE.nextSourceId++,
        room_id: Number(roomId),
        hardware_type: body.hardware_type,
        device_path: body.device_path || null,
        device_id: body.device_id || null,
        label: body.label,
        status: 'idle',
        is_active: true,
        health_status: 'healthy',
        config_json: null,
        created_at: new Date().toISOString(),
      };
      MOCK_STORE.sources.push(newSrc);
      return newSrc;
    }
  },

  async removeSource(
    roomId: number | string,
    sourceId: number | string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<void> {
    try {
      await request<void>(`/api/rooms/${roomId}/sources/${sourceId}`, {
        orgSlug,
        method: 'DELETE',
      });
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      MOCK_STORE.sources = MOCK_STORE.sources.filter(
        (s) => !(s.id === Number(sourceId) && s.room_id === Number(roomId)),
      );
    }
  },

  async generatePairingCode(roomId: number | string, { orgSlug }: RoomsApiOptions): Promise<PairingCode> {
    try {
      const raw = await request<Record<string, unknown>>(
        `/api/rooms/${roomId}/pairing-codes`,
        { orgSlug, method: 'POST', body: JSON.stringify({}) },
      );
      return {
        code: String(raw.code || ''),
        expires_at: String(raw.expires_at || ''),
        room_id: Number(raw.room_id) || Number(roomId) || 0,
      };
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const code: PairingCode = {
        code: generatePairingCode(),
        expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
        room_id: Number(roomId),
      };
      MOCK_STORE.pairing.set(Number(roomId), code);
      return code;
    }
  },

  /**
   * Redeem a pairing code. With ``deviceId`` set this is the Phase 3
   * satellite-enrolment path — the response carries a ``device_secret``
   * that MUST be persisted by the device on receipt; the server returns
   * it exactly once. Without ``deviceId`` this is the Phase 1 legacy
   * host-side flow (no secret issued).
   *
   * Wired here so the admin UI can drive a one-time secret reveal flow
   * for ops who need to enrol a device manually. Real devices will
   * usually redeem from their own firmware/agent.
   */
  async redeemPairingCode(
    code: string,
    {
      orgSlug,
      deviceId,
      deviceName,
      deviceType,
    }: RoomsApiOptions & {
      deviceId?: string;
      deviceName?: string;
      deviceType?: string;
    },
  ): Promise<PairingRedeemResult> {
    const body: Record<string, unknown> = { code };
    if (deviceId) body.device_id = deviceId;
    if (deviceName) body.device_name = deviceName;
    if (deviceType) body.device_type = deviceType;

    const raw = await request<Record<string, unknown>>(
      `/api/rooms/pairing-codes/redeem`,
      { orgSlug, method: 'POST', body: JSON.stringify(body) },
    );
    return {
      room_id: String(raw.room_id || ''),
      redeemed_at: String(raw.redeemed_at || ''),
      device_id: (raw.device_id as string | null | undefined) ?? null,
      organization_id:
        typeof raw.organization_id === 'number' ? raw.organization_id : null,
      device_secret: (raw.device_secret as string | null | undefined) ?? null,
      secret_warning:
        (raw.secret_warning as string | null | undefined) ?? null,
    };
  },

  async startRecording(roomId: number | string, { orgSlug }: RoomsApiOptions): Promise<{ session_id: string }> {
    try {
      const raw = await request<Record<string, unknown>>(
        `/api/rooms/${roomId}/recordings/start`,
        { orgSlug, method: 'POST', body: JSON.stringify({}) },
      );
      return { session_id: String(raw.session_id || raw.session_pk || '') };
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const idx = MOCK_STORE.rooms.findIndex((r) => r.id === Number(roomId));
      if (idx === -1) throw new Error('Room not found');
      const sessionId = `mock-${Date.now()}`;
      MOCK_STORE.rooms[idx] = {
        ...MOCK_STORE.rooms[idx],
        status: 'recording',
        current_session_id: sessionId,
        current_session_started_at: new Date().toISOString(),
        last_recording_at: new Date().toISOString(),
      };
      return { session_id: sessionId };
    }
  },

  async stopRecording(roomId: number | string, { orgSlug }: RoomsApiOptions): Promise<void> {
    try {
      await request<void>(`/api/rooms/${roomId}/recordings/stop`, {
        orgSlug,
        method: 'POST',
        body: JSON.stringify({}),
      });
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const idx = MOCK_STORE.rooms.findIndex((r) => r.id === Number(roomId));
      if (idx === -1) throw new Error('Room not found');
      MOCK_STORE.rooms[idx] = {
        ...MOCK_STORE.rooms[idx],
        status: 'idle',
        current_session_id: null,
        current_session_started_at: null,
      };
    }
  },

  async listAcl(roomId: number | string, { orgSlug }: RoomsApiOptions): Promise<RoomAclEntry[]> {
    try {
      const raw = await request<Array<Record<string, unknown>>>(`/api/rooms/${roomId}/acl`, {
        orgSlug,
        method: 'GET',
      });
      return raw.map((e) => ({
        user_id: Number(e.user_id),
        role: (e.role as RoomRole) || 'view',
        granted_at: (e.created_at as string) || (e.granted_at as string) || null,
        username: (e.username as string) || null,
        email: (e.email as string) || null,
        full_name: (e.full_name as string) || null,
      }));
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      return MOCK_STORE.acl.get(Number(roomId)) || [];
    }
  },

  async grantAcl(
    roomId: number | string,
    body: { user_id: number; role: RoomRole },
    { orgSlug }: RoomsApiOptions,
  ): Promise<RoomAclEntry> {
    try {
      const raw = await request<Record<string, unknown>>(`/api/rooms/${roomId}/acl`, {
        orgSlug,
        method: 'POST',
        body: JSON.stringify(body),
      });
      return {
        user_id: Number(raw.user_id),
        role: (raw.role as RoomRole) || body.role,
        granted_at: (raw.created_at as string) || (raw.granted_at as string) || null,
      };
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const existing = MOCK_STORE.acl.get(Number(roomId)) || [];
      const entry: RoomAclEntry = {
        user_id: body.user_id,
        role: body.role,
        granted_at: new Date().toISOString(),
      };
      const filtered = existing.filter((e) => e.user_id !== body.user_id);
      filtered.push(entry);
      MOCK_STORE.acl.set(Number(roomId), filtered);
      return entry;
    }
  },

  async revokeAcl(roomId: number | string, userId: number, { orgSlug }: RoomsApiOptions): Promise<void> {
    try {
      await request<void>(`/api/rooms/${roomId}/acl/${userId}`, {
        orgSlug,
        method: 'DELETE',
      });
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      const existing = MOCK_STORE.acl.get(Number(roomId)) || [];
      MOCK_STORE.acl.set(
        Number(roomId),
        existing.filter((e) => e.user_id !== userId),
      );
    }
  },

  async listAudioDevices({ orgSlug }: RoomsApiOptions): Promise<AudioDevice[]> {
    try {
      const raw = await request<Array<Record<string, unknown>>>(
        '/api/system/audio-devices',
        { orgSlug, method: 'GET' },
      );
      return raw.map((d) => ({
        device_path:
          typeof d.device_path === 'string'
            ? d.device_path
            : typeof d.card === 'number' && typeof d.device === 'number'
            ? `hw:${d.card},${d.device}`
            : String(d.device_path || ''),
        card_name: String(d.card_name || d.card || ''),
        device_name: String(d.device_name || d.name || ''),
      }));
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      return [
        {
          device_path: 'hw:0,0',
          card_name: 'HDA Intel PCH',
          device_name: 'ALC295 Analog',
        },
        {
          device_path: 'hw:2,0',
          card_name: 'USB Audio Device',
          device_name: 'Yeti Stereo Microphone',
        },
        {
          device_path: 'hw:3,0',
          card_name: 'Ceiling Mic Array',
          device_name: 'ReSpeaker 4-Mic',
        },
      ];
    }
  },

  /**
   * One-shot mic level probe. Records `durationSec` seconds (clamped 0.2–10)
   * from `devicePath` and returns RMS+peak dBFS. Used by the room
   * setup wizard's Test mic button and as a polling fallback for the
   * Live tab VU bar.
   *
   * If the backend is missing the route entirely we synthesize a "no
   * audio detected" response so the UI degrades gracefully — there's
   * no useful mock value when the device is server-side hardware.
   */
  async probeAudioDevice(
    devicePath: string,
    options: RoomsApiOptions & { durationSec?: number },
  ): Promise<AudioDeviceProbe> {
    const durationSec = Math.max(
      0.2,
      Math.min(10, options.durationSec ?? 3.0),
    );
    const encoded = encodeURIComponent(devicePath);
    const url = `/api/system/audio-devices/${encoded}/test?duration_sec=${durationSec}`;
    try {
      const raw = await request<Record<string, unknown>>(url, {
        orgSlug: options.orgSlug,
        method: 'GET',
      });
      return {
        device_path: String(raw.device_path || devicePath),
        duration_sec: Number(raw.duration_sec || durationSec),
        sample_count: Number(raw.sample_count || 0),
        rms_db: Number(raw.rms_db ?? -90),
        peak_db: Number(raw.peak_db ?? -90),
        detected_audio: !!raw.detected_audio,
      };
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      // Mock pretends a working mic so the UI flows are testable.
      return {
        device_path: devicePath,
        duration_sec: durationSec,
        sample_count: Math.round(durationSec * 16000),
        rms_db: -28 + (Math.random() - 0.5) * 4,
        peak_db: -12 + (Math.random() - 0.5) * 6,
        detected_audio: true,
      };
    }
  },

  /**
   * Returns the currently-recording session for the given room, or null
   * if the room is idle. The Live tab uses this to discover the session
   * id to poll transcripts/summaries against.
   */
  async getActiveSession(
    roomId: number | string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<RoomActiveSession> {
    try {
      const raw = await request<Record<string, unknown>>(
        `/api/rooms/${roomId}/active-session`,
        { orgSlug, method: 'GET' },
      );
      return {
        room_id: String(raw.room_id || roomId),
        session_id:
          typeof raw.session_id === 'string' ? raw.session_id : null,
        session_pk:
          typeof raw.session_pk === 'number' ? raw.session_pk : null,
        started_at:
          typeof raw.started_at === 'string' ? raw.started_at : null,
        status: typeof raw.status === 'string' ? raw.status : null,
      };
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      await pseudoSleep();
      return {
        room_id: String(roomId),
        session_id: null,
        session_pk: null,
        started_at: null,
        status: null,
      };
    }
  },

  /**
   * Poll the session GET endpoint for the in-progress transcript and
   * persisted summaries. The Live tab calls this every few seconds
   * while the room is recording.
   *
   * `sessionId` is the public session_id string (UUID) the recorder
   * uses on its chunk URLs.
   */
  async getLiveSession(
    sessionId: string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<LiveSessionPoll> {
    const raw = await request<Record<string, unknown>>(
      `/api/recording-sessions/${sessionId}`,
      { orgSlug, method: 'GET' },
    );
    return {
      id: String(raw.id || sessionId),
      session_id: String(raw.session_id || sessionId),
      title: String(raw.title || raw.name || ''),
      status: String(raw.status || 'unknown'),
      duration: Number(raw.duration || 0),
      transcript_simple: String(raw.transcript_simple || ''),
      transcript_diarized:
        (raw.transcript_diarized as LiveSessionPoll['transcript_diarized']) ||
        null,
      transcription_segments:
        (raw.transcription_segments as LiveSessionPoll['transcription_segments']) ||
        [],
      progressive_summaries:
        (raw.progressive_summaries as LiveSessionPoll['progressive_summaries']) ||
        [],
      metadata: (raw.metadata as Record<string, unknown>) || {},
      room_id: (raw.room_id as string | null | undefined) ?? null,
      room_source_id:
        (raw.room_source_id as string | null | undefined) ?? null,
      room_name: (raw.room_name as string | null | undefined) ?? null,
    };
  },

  /**
   * Read the persisted summary slice stack for a session. Room sessions
   * land here for both auto-triggered (from the room recorder) and
   * manually-triggered slices. Non-room sessions can also be read — they
   * just usually return an empty list because the browser still owns
   * their slice rolling.
   */
  async getSummarySlices(
    sessionId: string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<SummarySlicesResponse> {
    const raw = await request<Record<string, unknown>>(
      `/api/recordings/sessions/${sessionId}/summary-slices`,
      { orgSlug, method: 'GET' },
    );
    return {
      session_id: String(raw.session_id || sessionId),
      is_room_session: Boolean(raw.is_room_session),
      slices: Array.isArray(raw.slices)
        ? (raw.slices as SummarySlice[])
        : [],
      trigger_words: Number(raw.trigger_words || 500),
      max_per_session: Number(raw.max_per_session || 200),
    };
  },

  /**
   * Generate a new slice now ("Summarize now" button). The backend rolls
   * the slice from the current transcript window, persists into
   * processing_metadata, and returns the new slice. Returns 422 when
   * there isn't enough new transcript since the previous slice.
   */
  async createSummarySlice(
    sessionId: string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<{ slice: SummarySlice; session_id: string }> {
    const raw = await request<Record<string, unknown>>(
      `/api/recordings/sessions/${sessionId}/summary-slices`,
      { orgSlug, method: 'POST' },
    );
    return {
      slice: raw.slice as SummarySlice,
      session_id: String(raw.session_id || sessionId),
    };
  },

  /**
   * Open an SSE connection to the room-levels stream. Returns the
   * EventSource so the caller can attach handlers and close it.
   *
   * The browser's EventSource API can't send custom headers (no
   * Authorization, no X-MeetingOps-Org), so the backend route must
   * accept either a cookie-based session or a query-string token.
   * Our backend uses cookie + bearer; cookies travel with `withCredentials`.
   */
  openRoomLevelStream(
    roomId: number | string,
    { orgSlug }: RoomsApiOptions,
  ): EventSource {
    const params = new URLSearchParams();
    if (orgSlug) params.set('org', orgSlug);
    // Bearer-as-query is a one-off concession for EventSource (which
    // can't set headers). The room-levels endpoint accepts auth via
    // standard Depends — including the cookie — so this is best-effort.
    try {
      const token = localStorage.getItem('access_token');
      if (token) params.set('access_token', token);
    } catch {
      /* ignore storage failures */
    }
    const qs = params.toString();
    const url = `${config.apiBaseUrl}/api/rooms/${roomId}/levels${qs ? `?${qs}` : ''}`;
    return new EventSource(url, { withCredentials: true });
  },

  /**
   * Discard (hard-delete) a recording session. Uses the canonical
   * ORG-SCOPED `/api/simple/recording-sessions/{id}` DELETE handler (scopes
   * by active org via _get_session_for_org). The older `/api/recording-sessions/{id}`
   * route is not org-scoped, so a stray cross-org id could be deleted — repointed.
   */
  async discardSession(
    sessionId: string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<void> {
    await request<void>(`/api/simple/recording-sessions/${sessionId}`, {
      orgSlug,
      method: 'DELETE',
    });
  },

  async listOrgUsers({ orgSlug }: RoomsApiOptions): Promise<OrgUser[]> {
    // Spec said `GET /api/users/org` but that route doesn't exist yet.
    // `/api/auth/users` exists, gated on `user.read`, and returns the same
    // shape we need. Try the spec path first; fall back to /api/auth/users;
    // fall back to mock data last.
    try {
      return await request<OrgUser[]>('/api/users/org', { orgSlug, method: 'GET' });
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      try {
        return await request<OrgUser[]>('/api/auth/users', { orgSlug, method: 'GET' });
      } catch (err2) {
        if (!isMockFallback(err2)) throw err2;
        await pseudoSleep();
        return [
          { id: 1, username: 'aaron', email: 'aaron@magicunicorn.tech', full_name: 'Aaron Stransky', role: 'admin' },
          { id: 2, username: 'shafen', email: 'shafen@example.com', full_name: 'Shafen Khan', role: 'member' },
        ];
      }
    }
  },

  async listSessions(
    roomId: number | string,
    { orgSlug }: RoomsApiOptions,
  ): Promise<RecordingSessionSummary[]> {
    try {
      return await request<RecordingSessionSummary[]>(
        `/api/recordings/sessions/?room_id=${roomId}`,
        { orgSlug, method: 'GET' },
      );
    } catch (err) {
      if (!isMockFallback(err)) throw err;
      // Try the other known endpoint shape as a fallback.
      try {
        const page = await request<{ items: RecordingSessionSummary[] } | RecordingSessionSummary[]>(
          `/api/simple/recording-sessions?room_id=${roomId}`,
          { orgSlug, method: 'GET' },
        );
        return Array.isArray(page) ? page : page.items;
      } catch (err2) {
        if (!isMockFallback(err2)) throw err2;
        await pseudoSleep();
        if (Number(roomId) === 1) {
          return [
            {
              id: 'mock-session-prev-1',
              title: 'Engineering standup',
              created_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
              duration: 35 * 60,
              status: 'completed',
              summary_preview: 'Discussed Q3 priorities and the new recording stack.',
            },
            {
              id: 'mock-session-prev-2',
              title: 'Customer call: Honeycutt',
              created_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
              duration: 52 * 60,
              status: 'completed',
              summary_preview: 'Walked through content production cadence.',
            },
          ];
        }
        return [];
      }
    }
  },
};

export default roomsApi;
