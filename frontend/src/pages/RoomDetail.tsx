import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  Circle,
  Play,
  Square,
  Trash2,
  AlertCircle,
  AlertTriangle,
  Loader2,
  PowerOff,
  Mic,
  Shield,
  UserPlus,
  Edit2,
  X,
  Check,
  Plus,
  FilePlus,
} from 'lucide-react';
import { useOrg } from '../contexts/OrgContext';
import { useAuth } from '../contexts/AuthContext';
import roomsApi, {
  type AudioDevice,
  type LiveSessionPoll,
  type OrgUser,
  type PairingCode,
  type PairingRedeemResult,
  type RecordingSessionSummary,
  type Room,
  type RoomAclEntry,
  type RoomRole,
  type RoomSource,
} from '../services/roomsApi';
import ConfirmModal from '../components/ConfirmModal';
import AudioDeviceList from '../components/rooms/AudioDeviceList';
import PairingCodeDisplay from '../components/rooms/PairingCodeDisplay';
import DeviceSecretReveal from '../components/rooms/DeviceSecretReveal';
import RoomLevelMeter from '../components/rooms/RoomLevelMeter';
import RoomLiveTranscript from '../components/rooms/RoomLiveTranscript';
import RoomLiveSummary from '../components/rooms/RoomLiveSummary';
import { SkeletonBlock } from '../components/Skeleton';
import { cacheKey, useHydratedState } from '../utils/cachedState';
import { getLocalOnly } from '../services/privacyMode';

type TabId = 'live' | 'sessions' | 'settings';

const RETENTION_PRESETS: Array<{ label: string; days: number | null }> = [
  { label: '30 days', days: 30 },
  { label: '60 days', days: 60 },
  { label: '90 days', days: 90 },
  { label: '180 days', days: 180 },
  { label: '1 year', days: 365 },
  { label: 'Unlimited', days: null },
];

function isAdminRole(role?: string, isSuperuser?: boolean): boolean {
  if (isSuperuser) return true;
  return role === 'admin' || role === 'superuser';
}

function formatElapsed(startIso: string): string {
  const start = new Date(startIso).getTime();
  const sec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const hrs = Math.floor(sec / 3600);
  const mins = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function formatDuration(seconds?: number | null): string {
  if (!seconds || seconds <= 0) return '—';
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  if (hrs > 0) return `${hrs}h ${mins}m`;
  return `${mins}m`;
}

function getSourceDeviceLabel(source: Pick<RoomSource, 'device_path' | 'device_id' | 'hardware_type'>): string {
  return source.device_path || source.device_id || source.hardware_type;
}

function getDefaultDeviceLabel(device: AudioDevice): string {
  return device.device_name || device.card_name || device.device_path;
}

function getSourceStatusClass(status: RoomSource['status']): string {
  switch (status) {
    case 'recording':
      return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200';
    case 'error':
      return 'border-red-500/30 bg-red-500/10 text-red-200';
    case 'disabled':
      return 'border-zinc-700 bg-zinc-800 text-zinc-400';
    case 'idle':
    default:
      return 'border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-100';
  }
}

export default function RoomDetail() {
  const { id: idParam } = useParams<{ id: string }>();
  // The backend can return either int ids (rooms via SQLAlchemy auto pk)
  // or UUID strings. URL routing happily accepts either; we pass the
  // raw param straight through to the API client and only convert to
  // Number for ConfirmModal text and other purely-cosmetic uses.
  const id: string = idParam || '';
  const navigate = useNavigate();
  const { user } = useAuth();
  const { activeOrganization } = useOrg();
  const orgSlug = activeOrganization?.slug || null;
  const isAdmin = isAdminRole(activeOrganization?.role, user?.is_superuser);

  // Hydrate the room shell + sources from last-known-good cache so
  // refresh paints the page content immediately instead of replacing
  // the whole layout with "Loading room…".
  const [room, setRoom, roomHydrated] = useHydratedState<Room | null>(
    cacheKey('roomDetail.room', orgSlug, id),
    null,
  );
  const [sources, setSources, sourcesHydrated] = useHydratedState<RoomSource[]>(
    cacheKey('roomDetail.sources', orgSlug, id),
    [],
  );
  const hydrated = roomHydrated || sourcesHydrated;
  const [loading, setLoading] = useState(!hydrated);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeTab, setActiveTab] = useState<TabId>('live');
  const [privacyMode] = useState<boolean>(() => getLocalOnly());

  const fetchAll = useCallback(async () => {
    if (!orgSlug || !id) return;
    setError(null);
    try {
      const [r, s] = await Promise.all([
        roomsApi.get(id, { orgSlug }),
        roomsApi.listSources(id, { orgSlug }),
      ]);
      setRoom(r);
      setSources(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load room');
    } finally {
      setLoading(false);
    }
  }, [id, orgSlug, setRoom, setSources]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Poll live status every 5s
  useEffect(() => {
    if (privacyMode) return;
    const i = window.setInterval(() => {
      if (!busy) fetchAll();
    }, 5000);
    return () => window.clearInterval(i);
  }, [fetchAll, busy, privacyMode]);

  const roomApiId = (): number | string => {
    if (!room) return id;
    return room.raw_id || room.id;
  };

  const handleStart = async () => {
    if (!room) return;
    setBusy(true);
    setError(null);
    try {
      await roomsApi.startRecording(roomApiId(), { orgSlug });
      await fetchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start recording');
    } finally {
      setBusy(false);
    }
  };

  const handleStop = async () => {
    if (!room) return;
    setBusy(true);
    setError(null);
    try {
      await roomsApi.stopRecording(roomApiId(), { orgSlug });
      await fetchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to stop recording');
    } finally {
      setBusy(false);
    }
  };

  if (privacyMode) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-12 text-center text-zinc-300">
        Conference rooms aren't available in privacy mode.
      </div>
    );
  }

  if (loading && !room) {
    // Skeleton layout mirrors the real page shell so refresh shows
    // the structure immediately instead of "Loading room…" then a
    // hard jump to the populated UI.
    return (
      <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <SkeletonBlock className="h-8 w-8 rounded-lg" />
            <div className="space-y-2">
              <SkeletonBlock className="h-4 w-40" />
              <SkeletonBlock className="h-3 w-24" />
            </div>
          </div>
          <SkeletonBlock className="h-6 w-24 rounded-full" />
        </div>
        <div className="mb-4 flex gap-4 border-b border-zinc-800 pb-2">
          <SkeletonBlock className="h-4 w-12" />
          <SkeletonBlock className="h-4 w-16" />
          <SkeletonBlock className="h-4 w-16" />
        </div>
        <SkeletonBlock className="h-40 w-full rounded-2xl" />
      </div>
    );
  }

  if (error && !room) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-6 sm:px-6">
        <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
        <button
          type="button"
          onClick={() => navigate('/rooms')}
          className="mt-4 inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
        >
          <ArrowLeft className="h-4 w-4" /> Back to rooms
        </button>
      </div>
    );
  }

  if (!room) return null;

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => navigate('/rooms')}
            className="rounded-lg border border-zinc-700 bg-zinc-900 p-2 text-zinc-300 hover:bg-zinc-800"
            aria-label="Back to rooms"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div>
            <h1 className="text-xl font-semibold text-zinc-100">{room.name}</h1>
            {room.location && <p className="text-xs text-zinc-500">{room.location}</p>}
          </div>
        </div>
        <StatusPill room={room} />
      </div>

      {error && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
      )}

      {/* v3.19 (audit §7 a11y). Tab buttons had no ARIA — added
          `role="tablist"` / `role="tab"` / `aria-selected` /
          `aria-controls` and corresponding `role="tabpanel"` /
          `aria-labelledby` on the panels so screen readers + AT can
          announce + navigate this as a real tab interface. */}
      <div
        className="mb-4 flex gap-2 border-b border-zinc-800"
        role="tablist"
        aria-label="Room sections"
      >
        <TabButton id="live" current={activeTab} onClick={setActiveTab}>Live</TabButton>
        <TabButton id="sessions" current={activeTab} onClick={setActiveTab}>Sessions</TabButton>
        <TabButton id="settings" current={activeTab} onClick={setActiveTab}>Settings</TabButton>
      </div>

      {activeTab === 'live' && (
        <div
          role="tabpanel"
          id="room-panel-live"
          aria-labelledby="room-tab-live"
          tabIndex={0}
        >
          <LiveTab
            room={room}
            sources={sources}
            orgSlug={orgSlug}
            busy={busy}
            onStart={handleStart}
            onStop={handleStop}
            onRefresh={fetchAll}
          />
        </div>
      )}
      {activeTab === 'sessions' && (
        <div
          role="tabpanel"
          id="room-panel-sessions"
          aria-labelledby="room-tab-sessions"
          tabIndex={0}
        >
          <SessionsTab
            roomId={room.raw_id || room.id}
            orgSlug={orgSlug}
          />
        </div>
      )}
      {activeTab === 'settings' && (
        <div
          role="tabpanel"
          id="room-panel-settings"
          aria-labelledby="room-tab-settings"
          tabIndex={0}
        >
          <SettingsTab
            room={room}
            sources={sources}
            orgSlug={orgSlug}
            isAdmin={isAdmin}
            onChange={fetchAll}
          />
        </div>
      )}
    </div>
  );
}

// -- StatusPill ----------------------------------------------------------------

function StatusPill({ room }: { room: Room }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (room.status !== 'recording') return;
    const i = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(i);
  }, [room.status]);

  if (room.status === 'recording') {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-sm text-emerald-200">
        <Circle className="h-3 w-3 animate-pulse fill-emerald-400 text-emerald-400" />
        Recording
        {room.current_session_started_at && (
          <span className="font-mono tabular-nums text-emerald-100">
            · {formatElapsed(room.current_session_started_at)}
          </span>
        )}
      </div>
    );
  }
  if (room.status === 'error') {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-sm text-red-200">
        <AlertTriangle className="h-3.5 w-3.5" /> Error
      </div>
    );
  }
  if (room.status === 'offline') {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-sm text-amber-200">
        <PowerOff className="h-3.5 w-3.5" /> Offline
      </div>
    );
  }
  return (
    <div className="inline-flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300">
      <Circle className="h-3 w-3 text-zinc-500" /> Idle
    </div>
  );
}

function TabButton({
  id,
  current,
  onClick,
  children,
}: {
  id: TabId;
  current: TabId;
  onClick: (id: TabId) => void;
  children: React.ReactNode;
}) {
  const active = current === id;
  return (
    <button
      type="button"
      role="tab"
      id={`room-tab-${id}`}
      aria-selected={active}
      aria-controls={`room-panel-${id}`}
      tabIndex={active ? 0 : -1}
      onClick={() => onClick(id)}
      className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-fuchsia-500 ${
        active
          ? 'border-fuchsia-500 text-fuchsia-100'
          : 'border-transparent text-zinc-400 hover:text-zinc-200'
      }`}
    >
      {children}
    </button>
  );
}

// -- LiveTab -------------------------------------------------------------------

function LiveTab({
  room,
  sources,
  orgSlug,
  busy,
  onStart,
  onStop,
  onRefresh,
}: {
  room: Room;
  sources: RoomSource[];
  orgSlug: string | null;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
  onRefresh: () => Promise<void>;
}) {
  const isRecording = room.status === 'recording';
  const activeSources = sources.filter((s) => s.status !== 'disabled');
  const apiId: number | string = room.raw_id || room.id;
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [livePoll, setLivePoll] = useState<LiveSessionPoll | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [discardBusy, setDiscardBusy] = useState(false);
  const [discardError, setDiscardError] = useState<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  sessionIdRef.current = activeSessionId;

  // Discover the in-progress session id while recording. Cleared when
  // recording stops so the transcript pane wipes cleanly.
  useEffect(() => {
    if (!isRecording) {
      setActiveSessionId(null);
      setLivePoll(null);
      return;
    }
    let cancelled = false;
    const fetchSession = async () => {
      try {
        const reply = await roomsApi.getActiveSession(apiId, { orgSlug });
        if (!cancelled) setActiveSessionId(reply.session_id);
      } catch {
        // Best-effort. The transcript pane handles null gracefully.
      }
    };
    fetchSession();
    // Re-probe every 8 s in case the parent's room-level poll is slow
    // to pick up a freshly-started session.
    const i = window.setInterval(fetchSession, 8000);
    return () => {
      cancelled = true;
      window.clearInterval(i);
    };
  }, [isRecording, apiId, orgSlug]);

  const handleDiscard = async () => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    setDiscardBusy(true);
    setDiscardError(null);
    try {
      // Stop the recording first so the recorder releases the device,
      // then delete the session. We don't surface the stop failure
      // beyond a log — if it fails, the discard still hard-deletes the
      // row.
      try {
        await roomsApi.stopRecording(apiId, { orgSlug });
      } catch {
        // ignore; the delete handles the cleanup
      }
      await roomsApi.discardSession(sid, { orgSlug });
      await onRefresh();
      setDiscardOpen(false);
    } catch (err) {
      setDiscardError(err instanceof Error ? err.message : 'Discard failed');
    } finally {
      setDiscardBusy(false);
    }
  };

  const handleNewSession = async () => {
    try {
      await roomsApi.stopRecording(apiId, { orgSlug });
    } catch {
      /* best-effort */
    }
    await onRefresh();
    // Auto-start a fresh session after the stop completes.
    try {
      await roomsApi.startRecording(apiId, { orgSlug });
      await onRefresh();
    } catch (err) {
      setDiscardError(err instanceof Error ? err.message : 'New session failed');
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <div className="flex flex-wrap items-center gap-3">
          {isRecording ? (
            <>
              <button
                type="button"
                onClick={onStop}
                disabled={busy}
                className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                <Square className="h-4 w-4" /> Stop & save
              </button>
              <button
                type="button"
                onClick={handleNewSession}
                disabled={busy}
                className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
              >
                <FilePlus className="h-4 w-4" /> New session
              </button>
              <button
                type="button"
                onClick={() => setDiscardOpen(true)}
                disabled={busy || !activeSessionId}
                className="inline-flex items-center gap-2 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm font-medium text-red-200 hover:bg-red-500/20 disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" /> Discard
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={onStart}
              disabled={busy || sources.length === 0}
              className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
            >
              <Play className="h-4 w-4" /> Start recording
            </button>
          )}
          {sources.length === 0 && (
            <span className="text-xs text-amber-300">
              Add an audio source in Settings before starting.
            </span>
          )}
        </div>
        {discardError && (
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
            <AlertCircle className="h-3.5 w-3.5" /> {discardError}
          </div>
        )}
      </div>

      <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <h2 className="text-sm font-semibold text-zinc-100">Audio sources</h2>
        {activeSources.length === 0 ? (
          <p className="mt-2 text-sm text-zinc-500">
            No active sources. Add one in Settings.
          </p>
        ) : (
          <ul className="mt-3 flex flex-col gap-3">
            {activeSources.map((src) => (
              <li
                key={src.id}
                className="rounded-lg border border-zinc-800 bg-black/30 px-3 py-2.5 text-sm"
              >
                <div className="flex items-center justify-between">
                  <div className="flex min-w-0 items-center gap-2">
                    <Mic className="h-4 w-4 shrink-0 text-zinc-400" />
                    <span className="truncate text-zinc-100">{src.label}</span>
                    <span className="truncate text-xs text-zinc-500">
                      {getSourceDeviceLabel(src)}
                    </span>
                  </div>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${
                      getSourceStatusClass(src.status)
                    }`}
                  >
                    {src.status}
                  </span>
                </div>
                <div className="mt-2">
                  <RoomLevelMeter
                    roomId={apiId}
                    orgSlug={orgSlug}
                    recording={isRecording}
                    label={src.label || getSourceDeviceLabel(src) || 'Live level'}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <RoomLiveTranscript
          sessionId={activeSessionId}
          orgSlug={orgSlug}
          recording={isRecording}
          onPoll={(s) => setLivePoll(s)}
        />
        <RoomLiveSummary
          sessionId={activeSessionId}
          orgSlug={orgSlug}
          recording={isRecording}
          transcript={livePoll?.transcript_simple || ''}
          // RoomDetail only renders for conference-room recordings, so
          // we always opt in to the server-rolled slice store here. The
          // `room_id` field on the live poll is what marks a session
          // as room-owned upstream — we trust the URL having brought us
          // to this page in lieu of waiting for the first poll to land.
          isRoomSession={true}
        />
      </div>

      <ConfirmModal
        isOpen={discardOpen}
        title="Discard this session?"
        description={
          <span>
            This stops the recording and permanently deletes the current
            session, including any audio and partial transcript already
            captured. This cannot be undone.
          </span>
        }
        confirmLabel={discardBusy ? 'Discarding…' : 'Discard'}
        tone="danger"
        onConfirm={handleDiscard}
        onCancel={() => setDiscardOpen(false)}
      />
    </div>
  );
}

// -- SessionsTab ---------------------------------------------------------------

function SessionsTab({
  roomId,
  orgSlug,
}: {
  roomId: number | string;
  orgSlug: string | null;
}) {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<RecordingSessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    roomsApi
      .listSessions(roomId, { orgSlug })
      .then((data) => {
        if (!cancelled) setSessions(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load sessions');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [roomId, orgSlug]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-zinc-400">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading sessions…
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
        <AlertCircle className="h-4 w-4" /> {error}
      </div>
    );
  }
  if (sessions.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-zinc-800 bg-zinc-950/40 px-6 py-10 text-center text-sm text-zinc-500">
        No sessions yet. Start a recording from the Live tab.
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-2">
      {sessions.map((s) => (
        <li
          key={s.id}
          className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4 hover:bg-zinc-900/60"
        >
          <button
            type="button"
            onClick={() => navigate(`/sessions/${s.id}`)}
            className="w-full text-left"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div className="text-sm font-semibold text-zinc-100 truncate">
                {s.title || 'Untitled session'}
              </div>
              <div className="text-xs text-zinc-500">
                {s.created_at ? new Date(s.created_at).toLocaleString() : '—'} ·{' '}
                {formatDuration(s.duration)}
              </div>
            </div>
            {s.summary_preview && (
              <p className="mt-1 text-xs leading-5 text-zinc-400 line-clamp-2">
                {s.summary_preview}
              </p>
            )}
          </button>
        </li>
      ))}
    </ul>
  );
}

// -- SettingsTab ---------------------------------------------------------------

function SettingsTab({
  room,
  sources,
  orgSlug,
  isAdmin,
  onChange,
}: {
  room: Room;
  sources: RoomSource[];
  orgSlug: string | null;
  isAdmin: boolean;
  onChange: () => Promise<void>;
}) {
  const [editName, setEditName] = useState(false);
  const [name, setName] = useState(room.name);
  const [location, setLocation] = useState(room.location || '');
  const [savingMeta, setSavingMeta] = useState(false);
  const [retention, setRetention] = useState<number | null>(room.retention_days);
  const [legalHold, setLegalHold] = useState(room.legal_hold);
  const [pairing, setPairing] = useState<PairingCode | null>(null);
  const [pairingLoading, setPairingLoading] = useState(false);
  const [manualDeviceId, setManualDeviceId] = useState('');
  const [revealedSecret, setRevealedSecret] = useState<PairingRedeemResult | null>(null);
  const [redeemLoading, setRedeemLoading] = useState(false);
  const [showAddSource, setShowAddSource] = useState(false);
  const [pendingDevices, setPendingDevices] = useState<AudioDevice[]>([]);
  const [pendingLabels, setPendingLabels] = useState<Record<string, string>>({});
  const [addingSources, setAddingSources] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmLegalHold, setConfirmLegalHold] = useState<boolean | null>(null);
  const [acl, setAcl] = useState<RoomAclEntry[]>([]);
  const [aclUsers, setAclUsers] = useState<OrgUser[]>([]);
  const [grantUserId, setGrantUserId] = useState<number | ''>('');
  const [grantRole, setGrantRole] = useState<RoomRole>('view');
  const [aclError, setAclError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  // Prefer the upstream string id (UUID safe) for every API call.
  const apiId: number | string = room.raw_id || room.id;
  const roomSourceDevicePaths = useMemo(
    () => sources.map((src) => src.device_path).filter((path): path is string => !!path),
    [sources],
  );

  // Load ACL + users for admin view.
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    Promise.all([
      roomsApi.listAcl(apiId, { orgSlug }),
      roomsApi.listOrgUsers({ orgSlug }),
    ])
      .then(([a, u]) => {
        if (!cancelled) {
          setAcl(a);
          setAclUsers(u);
        }
      })
      .catch((err) => {
        if (!cancelled) setAclError(err instanceof Error ? err.message : 'Failed to load access list');
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin, apiId, orgSlug]);

  const aclUserMap = useMemo(() => {
    const m = new Map<number, OrgUser>();
    aclUsers.forEach((u) => m.set(u.id, u));
    return m;
  }, [aclUsers]);

  const saveMeta = async (overrides?: { name?: string; location?: string | null }) => {
    setSavingMeta(true);
    setError(null);
    try {
      await roomsApi.update(
        apiId,
        {
          name: overrides?.name ?? name,
          location:
            overrides?.location !== undefined
              ? overrides.location
              : location.trim() || null,
        },
        { orgSlug },
      );
      await onChange();
      setEditName(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSavingMeta(false);
    }
  };

  const saveRetention = async (days: number | null) => {
    setRetention(days);
    try {
      await roomsApi.update(apiId, { retention_days: days }, { orgSlug });
      await onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update retention');
    }
  };

  const toggleLegalHold = async (val: boolean) => {
    try {
      await roomsApi.update(apiId, { legal_hold: val }, { orgSlug });
      setLegalHold(val);
      await onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle legal hold');
    } finally {
      setConfirmLegalHold(null);
    }
  };

  const generatePairingCode = async () => {
    setPairingLoading(true);
    try {
      const code = await roomsApi.generatePairingCode(apiId, { orgSlug });
      setPairing(code);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate pairing code');
    } finally {
      setPairingLoading(false);
    }
  };

  const redeemManualPair = async () => {
    if (!pairing) return;
    setRedeemLoading(true);
    try {
      const result = await roomsApi.redeemPairingCode(pairing.code, {
        orgSlug,
        deviceId: manualDeviceId.trim() || undefined,
      });
      setRevealedSecret(result);
      setPairing(null);
      setManualDeviceId('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to redeem pairing code');
    } finally {
      setRedeemLoading(false);
    }
  };

  const handlePendingDevicesChange = (nextDevices: AudioDevice[]) => {
    setPendingDevices(nextDevices);
    setPendingLabels((current) => {
      const next: Record<string, string> = {};
      nextDevices.forEach((device) => {
        next[device.device_path] = current[device.device_path] || getDefaultDeviceLabel(device);
      });
      return next;
    });
  };

  const clearPendingSources = () => {
    setPendingDevices([]);
    setPendingLabels({});
  };

  const addSources = async () => {
    if (pendingDevices.length === 0) return;
    setAddingSources(true);
    try {
      for (const device of pendingDevices) {
        await roomsApi.addSource(
          apiId,
          {
            hardware_type: 'server_usb_mic',
            device_path: device.device_path,
            label: pendingLabels[device.device_path]?.trim() || getDefaultDeviceLabel(device),
          },
          { orgSlug },
        );
      }
      setShowAddSource(false);
      clearPendingSources();
      await onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add source');
    } finally {
      setAddingSources(false);
    }
  };

  const removeSource = async (sourceId: number | string) => {
    try {
      await roomsApi.removeSource(apiId, sourceId, { orgSlug });
      await onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove source');
    }
  };

  const grantAcl = async () => {
    if (typeof grantUserId !== 'number') return;
    try {
      const entry = await roomsApi.grantAcl(
        apiId,
        { user_id: grantUserId, role: grantRole },
        { orgSlug },
      );
      setAcl((prev) => {
        const filtered = prev.filter((e) => e.user_id !== entry.user_id);
        filtered.push(entry);
        return filtered;
      });
      setGrantUserId('');
    } catch (err) {
      setAclError(err instanceof Error ? err.message : 'Grant failed');
    }
  };

  const revokeAcl = async (userId: number) => {
    try {
      await roomsApi.revokeAcl(apiId, userId, { orgSlug });
      setAcl((prev) => prev.filter((e) => e.user_id !== userId));
    } catch (err) {
      setAclError(err instanceof Error ? err.message : 'Revoke failed');
    }
  };

  const deleteRoom = async () => {
    try {
      await roomsApi.remove(apiId, { orgSlug });
      navigate('/rooms');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
      setConfirmDelete(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
      )}

      {/* Basics */}
      <section className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <h2 className="text-sm font-semibold text-zinc-100">Basics</h2>
        <div className="mt-3 flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <div className="flex-1">
              <label className="block text-[10px] uppercase tracking-wide text-zinc-500">Name</label>
              {editName ? (
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100"
                />
              ) : (
                <div className="mt-1 text-sm text-zinc-100">{room.name}</div>
              )}
            </div>
            {isAdmin &&
              (editName ? (
                <div className="flex gap-1">
                  <button
                    type="button"
                    onClick={() => saveMeta({ name })}
                    disabled={savingMeta || !name.trim()}
                    className="rounded-lg bg-emerald-600 p-1.5 text-white hover:bg-emerald-500 disabled:opacity-50"
                  >
                    <Check className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setEditName(false);
                      setName(room.name);
                    }}
                    className="rounded-lg border border-zinc-700 bg-zinc-900 p-1.5 text-zinc-200 hover:bg-zinc-800"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setEditName(true)}
                  className="rounded-lg border border-zinc-700 bg-zinc-900 p-1.5 text-zinc-300 hover:bg-zinc-800"
                >
                  <Edit2 className="h-4 w-4" />
                </button>
              ))}
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wide text-zinc-500">Location</label>
            <input
              type="text"
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              onBlur={() => saveMeta({ location: location.trim() || null })}
              disabled={!isAdmin || savingMeta}
              placeholder="Building B, Floor 3"
              className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 disabled:opacity-60"
            />
          </div>
        </div>
      </section>

      {/* Audio sources */}
      <section className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-zinc-100">Audio sources</h2>
          {isAdmin && (
            <button
              type="button"
              onClick={() => setShowAddSource((v) => !v)}
              className="inline-flex items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
            >
              <Plus className="h-3 w-3" /> Add source
            </button>
          )}
        </div>

        {sources.length === 0 ? (
          <p className="mt-2 text-sm text-zinc-500">
            No audio sources configured yet. Add one to enable recording.
          </p>
        ) : (
          <ul className="mt-3 flex flex-col gap-2">
            {sources.map((src) => (
              <li
                key={src.id}
                className="rounded-lg border border-zinc-800 bg-black/30 px-3 py-2 text-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Mic className="h-4 w-4 shrink-0 text-zinc-400" />
                      <span className="truncate text-zinc-100">{src.label}</span>
                      <span
                        className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide ${getSourceStatusClass(src.status)}`}
                      >
                        {src.status}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                      <span className="font-mono">{getSourceDeviceLabel(src)}</span>
                      <span className="rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 uppercase tracking-wide text-zinc-400">
                        {src.hardware_type}
                      </span>
                    </div>
                    {src.device_id && (
                      <div className="mt-1 truncate font-mono text-[11px] text-zinc-600">
                        device_id: {src.device_id}
                      </div>
                    )}
                  </div>
                  {isAdmin && (
                    <button
                      type="button"
                      onClick={() => removeSource(src.raw_id || src.id)}
                      className="rounded-lg p-1.5 text-zinc-400 hover:bg-red-500/10 hover:text-red-300"
                      aria-label="Remove source"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}

        {showAddSource && isAdmin && (
          <div className="mt-3 rounded-xl border border-zinc-800 bg-black/30 p-3">
            <AudioDeviceList
              orgSlug={orgSlug}
              selectionMode="multiple"
              selectedDevicePaths={pendingDevices.map((device) => device.device_path)}
              onSelectionChange={handlePendingDevicesChange}
              disabledDevicePaths={roomSourceDevicePaths}
            />
            <div className="mt-3 text-xs text-zinc-500">
              Select one or more microphones. Each source gets its own label before it is added.
            </div>
            {pendingDevices.length > 0 && (
              <div className="mt-3 flex flex-col gap-3">
                <div className="grid gap-2">
                  {pendingDevices.map((device) => (
                    <div
                      key={device.device_path}
                      className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm text-zinc-100">
                            {device.device_name || device.card_name}
                          </div>
                          <div className="mt-0.5 truncate font-mono text-xs text-zinc-500">
                            {device.card_name && device.card_name !== device.device_name
                              ? `${device.card_name}  ·  `
                              : ''}
                            {device.device_path}
                          </div>
                        </div>
                        <button
                          type="button"
                          onClick={() =>
                            handlePendingDevicesChange(
                              pendingDevices.filter((item) => item.device_path !== device.device_path),
                            )
                          }
                          className="rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-300 hover:bg-zinc-800"
                        >
                          Remove
                        </button>
                      </div>
                      <label className="mt-3 block text-[10px] uppercase tracking-wide text-zinc-500">
                        Source label
                      </label>
                      <input
                        type="text"
                        value={pendingLabels[device.device_path] || ''}
                        onChange={(e) =>
                          setPendingLabels((current) => ({
                            ...current,
                            [device.device_path]: e.target.value,
                          }))
                        }
                        placeholder="Podium, Audience, Speaker phone"
                        className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100"
                      />
                    </div>
                  ))}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-xs text-zinc-500">
                    Labels are saved with each source and can be edited later in the room config.
                  </p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setShowAddSource(false);
                        clearPendingSources();
                      }}
                      className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={addSources}
                      disabled={addingSources || pendingDevices.length === 0}
                      className="rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
                    >
                      {addingSources
                        ? 'Adding…'
                        : pendingDevices.length === 1
                        ? 'Add source'
                        : `Add ${pendingDevices.length} sources`}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </section>

      {/* Retention */}
      <section className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <h2 className="text-sm font-semibold text-zinc-100">Retention</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          {RETENTION_PRESETS.map((p) => (
            <button
              key={p.label}
              type="button"
              disabled={!isAdmin || legalHold}
              onClick={() => saveRetention(p.days)}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium ${
                retention === p.days
                  ? 'border-fuchsia-500/60 bg-fuchsia-500/10 text-fuchsia-100'
                  : 'border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800'
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              {p.label}
            </button>
          ))}
        </div>
        {legalHold && (
          <p className="mt-2 text-xs text-amber-300">
            Retention is paused while legal hold is on.
          </p>
        )}
      </section>

      {/* Legal hold */}
      {isAdmin && (
        <section className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
            <Shield className="h-4 w-4" /> Legal hold
          </h2>
          <div className="mt-3 flex items-center justify-between gap-3">
            <p className="text-sm leading-6 text-zinc-400">
              Legal hold disables automatic deletion of all recordings from this room.
              Required for litigation, investigations, and FOIA-style holds.
            </p>
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={legalHold}
                onChange={(e) => setConfirmLegalHold(e.target.checked)}
                className="h-4 w-4 accent-fuchsia-500"
              />
              <span className="text-sm text-zinc-200">{legalHold ? 'On' : 'Off'}</span>
            </label>
          </div>
        </section>
      )}

      {/* Pairing codes */}
      {isAdmin && (
        <section className="space-y-3">
          <PairingCodeDisplay
            code={pairing}
            onGenerate={generatePairingCode}
            loading={pairingLoading}
          />
          {pairing && !revealedSecret && (
            <details className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
              <summary className="cursor-pointer text-sm text-zinc-300 hover:text-zinc-100">
                Pair a device manually (ops / testing)
              </summary>
              <div className="mt-3 space-y-3">
                <p className="text-xs text-zinc-500">
                  Real satellite devices redeem from their own firmware. Use this only to test the
                  pairing flow without hardware, or to manually enrol a device you'll configure by
                  pasting its secret elsewhere.
                </p>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={manualDeviceId}
                    onChange={(e) => setManualDeviceId(e.target.value)}
                    placeholder="device_id (optional — backend assigns one if blank)"
                    className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500"
                  />
                  <button
                    type="button"
                    onClick={redeemManualPair}
                    disabled={redeemLoading}
                    className="rounded-lg bg-fuchsia-600 px-4 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
                  >
                    {redeemLoading ? 'Redeeming...' : 'Redeem'}
                  </button>
                </div>
              </div>
            </details>
          )}
          {revealedSecret?.device_secret && (
            <DeviceSecretReveal
              secret={revealedSecret.device_secret}
              deviceId={revealedSecret.device_id || 'unknown'}
              warning={revealedSecret.secret_warning}
              onDismiss={() => setRevealedSecret(null)}
            />
          )}
        </section>
      )}

      {/* ACL editor */}
      {isAdmin && (
        <section className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
            <UserPlus className="h-4 w-4" /> Per-user access
          </h2>
          {aclError && (
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <AlertCircle className="h-3 w-3" /> {aclError}
            </div>
          )}

          {acl.length === 0 ? (
            <p className="mt-2 text-sm text-zinc-500">
              No custom grants. Org members follow the default room visibility.
            </p>
          ) : (
            <ul className="mt-3 flex flex-col gap-2">
              {acl.map((e) => {
                const u = aclUserMap.get(e.user_id);
                return (
                  <li
                    key={e.user_id}
                    className="flex items-center justify-between rounded-lg border border-zinc-800 bg-black/30 px-3 py-2 text-sm"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-zinc-100">
                        {u?.full_name || u?.username || e.username || `user #${e.user_id}`}
                      </div>
                      <div className="text-xs text-zinc-500">
                        {u?.email || e.email || ''}
                      </div>
                    </div>
                    <select
                      value={e.role}
                      onChange={async (ev) => {
                        try {
                          await roomsApi.grantAcl(
                            apiId,
                            { user_id: e.user_id, role: ev.target.value as RoomRole },
                            { orgSlug },
                          );
                          setAcl((prev) =>
                            prev.map((p) =>
                              p.user_id === e.user_id ? { ...p, role: ev.target.value as RoomRole } : p,
                            ),
                          );
                        } catch (err) {
                          setAclError(err instanceof Error ? err.message : 'Update failed');
                        }
                      }}
                      className="rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
                    >
                      <option value="view">View</option>
                      <option value="record">Record</option>
                      <option value="manage">Manage</option>
                    </select>
                    <button
                      type="button"
                      onClick={() => revokeAcl(e.user_id)}
                      className="rounded-lg p-1 text-zinc-400 hover:bg-red-500/10 hover:text-red-300"
                      aria-label="Revoke access"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="mt-3 flex flex-wrap items-end gap-2">
            <div className="flex-1 min-w-[180px]">
              <label className="block text-[10px] uppercase tracking-wide text-zinc-500">User</label>
              <select
                value={grantUserId === '' ? '' : String(grantUserId)}
                onChange={(e) => setGrantUserId(e.target.value === '' ? '' : Number(e.target.value))}
                className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-2 text-sm text-zinc-100"
              >
                <option value="">Pick a user…</option>
                {aclUsers
                  .filter((u) => !acl.some((a) => a.user_id === u.id))
                  .map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name || u.username} ({u.email || u.username})
                    </option>
                  ))}
              </select>
            </div>
            <div>
              <label className="block text-[10px] uppercase tracking-wide text-zinc-500">Role</label>
              <select
                value={grantRole}
                onChange={(e) => setGrantRole(e.target.value as RoomRole)}
                className="mt-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-2 text-sm text-zinc-100"
              >
                <option value="view">View</option>
                <option value="record">Record</option>
                <option value="manage">Manage</option>
              </select>
            </div>
            <button
              type="button"
              onClick={grantAcl}
              disabled={grantUserId === ''}
              className="rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-3 py-2 text-xs font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
            >
              Grant
            </button>
          </div>
        </section>
      )}

      {/* Danger zone */}
      {isAdmin && (
        <section className="rounded-2xl border border-red-500/30 bg-red-500/5 p-5">
          <h2 className="text-sm font-semibold text-red-200">Danger zone</h2>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm leading-6 text-zinc-300">
              Deleting a room cascades to its audio sources, ACL entries, and pairing codes.
              Existing recordings stay attributed to the room name but lose live binding.
            </p>
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-3 py-2 text-xs font-medium text-white hover:bg-red-500"
            >
              <Trash2 className="h-4 w-4" /> Delete room
            </button>
          </div>
        </section>
      )}

      <ConfirmModal
        isOpen={confirmDelete}
        title="Delete this room?"
        description={
          <span>
            This will delete <span className="font-semibold">{room.name}</span> and all of its
            audio source bindings. Recording sessions stay in the database but lose their live
            room pointer. This cannot be undone.
          </span>
        }
        confirmLabel="Delete"
        tone="danger"
        onConfirm={deleteRoom}
        onCancel={() => setConfirmDelete(false)}
      />

      <ConfirmModal
        isOpen={confirmLegalHold !== null}
        title={confirmLegalHold ? 'Enable legal hold?' : 'Disable legal hold?'}
        description={
          confirmLegalHold ? (
            <span>
              While legal hold is on, no recording from this room can be auto-deleted, regardless
              of the retention policy. Use this for litigation, investigations, or compliance
              holds.
            </span>
          ) : (
            <span>
              Disabling legal hold re-enables automatic deletion based on the room's retention
              policy. Existing eligible recordings can be deleted within minutes.
            </span>
          )
        }
        confirmLabel={confirmLegalHold ? 'Enable hold' : 'Disable hold'}
        tone={confirmLegalHold ? 'primary' : 'danger'}
        onConfirm={() => toggleLegalHold(!!confirmLegalHold)}
        onCancel={() => setConfirmLegalHold(null)}
      />
    </div>
  );
}
