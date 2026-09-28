import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Plus,
  Search,
  Filter,
  DoorOpen,
  AlertCircle,
  Lock as LockIcon,
} from 'lucide-react';
import { useOrg } from '../contexts/OrgContext';
import roomsApi, { isMockMode, type Room } from '../services/roomsApi';
import RoomCard from '../components/rooms/RoomCard';
import { SkeletonRoomGrid } from '../components/Skeleton';
import { cacheKey, useHydratedState } from '../utils/cachedState';
import { getLocalOnly } from '../services/privacyMode';

type StatusFilter = 'all' | 'recording' | 'idle' | 'error';

export default function Rooms() {
  const { activeOrganization } = useOrg();
  const navigate = useNavigate();

  const orgSlug = activeOrganization?.slug || null;
  const [rooms, setRooms, hydratedFromCache] = useHydratedState<Room[]>(
    cacheKey('rooms', orgSlug),
    [],
  );
  // Only show the "no data yet, show skeleton" state on the very first
  // fetch when there's no cached data. Once we have rooms (cached or
  // fresh), background fetches must never wipe them.
  const [initialFetchPending, setInitialFetchPending] = useState(!hydratedFromCache);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [busyRoomId, setBusyRoomId] = useState<number | null>(null);
  const [privacyMode] = useState<boolean>(() => getLocalOnly());
  const [mockBanner, setMockBanner] = useState(false);

  const fetchRooms = useCallback(async () => {
    if (!orgSlug) return;
    setError(null);
    try {
      const data = await roomsApi.list({ orgSlug });
      setRooms(data);
      setMockBanner(isMockMode());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load rooms');
    } finally {
      setInitialFetchPending(false);
    }
  }, [orgSlug, setRooms]);

  useEffect(() => {
    fetchRooms();
  }, [fetchRooms]);

  // Light polling — every 10 s. Cheap because list endpoint is small.
  useEffect(() => {
    if (privacyMode) return;
    const i = window.setInterval(() => {
      if (!busyRoomId) fetchRooms();
    }, 10_000);
    return () => window.clearInterval(i);
  }, [fetchRooms, busyRoomId, privacyMode]);

  const handleStart = async (room: Room) => {
    setBusyRoomId(room.id);
    try {
      await roomsApi.startRecording(room.id, { orgSlug });
      await fetchRooms();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start recording');
    } finally {
      setBusyRoomId(null);
    }
  };

  const handleStop = async (room: Room) => {
    setBusyRoomId(room.id);
    try {
      await roomsApi.stopRecording(room.id, { orgSlug });
      await fetchRooms();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to stop recording');
    } finally {
      setBusyRoomId(null);
    }
  };

  const filteredRooms = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rooms.filter((r) => {
      if (q) {
        const hay = `${r.name} ${r.location || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (statusFilter === 'all') return true;
      if (statusFilter === 'recording') return r.status === 'recording';
      if (statusFilter === 'idle') return r.status === 'idle' || r.status === 'paused';
      if (statusFilter === 'error') return r.status === 'error' || r.status === 'offline';
      return true;
    });
  }, [rooms, search, statusFilter]);

  if (privacyMode) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-12 text-center">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-zinc-700 bg-zinc-900">
          <LockIcon className="h-6 w-6 text-zinc-400" />
        </div>
        <h1 className="text-xl font-semibold text-zinc-100">Conference rooms aren't available in privacy mode</h1>
        <p className="mt-2 text-sm leading-6 text-zinc-400">
          Privacy mode keeps every byte of audio inside your browser. Conference rooms are
          fundamentally server-side — a physical mic in a room captures audio, sends it to the
          server, and the server runs the transcription and summary pipelines for everyone with
          access to the room.
        </p>
        <p className="mt-2 text-sm leading-6 text-zinc-400">
          Turn privacy mode off in Settings to set up or view rooms in this organization.
        </p>
        <Link
          to="/settings"
          className="mt-5 inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 hover:bg-zinc-800"
        >
          Open settings
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <div className="flex flex-wrap items-center justify-between gap-3 pb-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-indigo-500">
            <DoorOpen className="h-5 w-5 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-zinc-100">Conference rooms</h1>
            <p className="text-xs text-zinc-500">
              Physical rooms with always-on or admin-triggered recording.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => navigate('/rooms/new')}
          className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500"
        >
          <Plus className="h-4 w-4" />
          New room
        </button>
      </div>

      {mockBanner && (
        <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          Backend rooms API isn't deployed yet — showing sample data and accepting writes only in
          your browser. They'll persist once the backend lands.
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 pb-4">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search rooms…"
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 py-2 pl-9 pr-3 text-sm text-zinc-100 outline-none focus:border-zinc-500"
          />
        </div>
        <div className="flex items-center gap-2 text-sm text-zinc-300">
          <Filter className="h-4 w-4 text-zinc-500" />
          {(['all', 'recording', 'idle', 'error'] as StatusFilter[]).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatusFilter(s)}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium capitalize ${
                statusFilter === s
                  ? 'border-fuchsia-500/60 bg-fuchsia-500/10 text-fuchsia-100'
                  : 'border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
      )}

      {initialFetchPending && rooms.length === 0 ? (
        <SkeletonRoomGrid count={3} />
      ) : filteredRooms.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-zinc-800 bg-zinc-950/40 px-6 py-12 text-center">
          <DoorOpen className="mx-auto h-10 w-10 text-zinc-600" />
          <h2 className="mt-3 text-base font-semibold text-zinc-100">
            {rooms.length === 0 ? 'No rooms yet' : 'No rooms match this filter'}
          </h2>
          <p className="mt-1 text-sm text-zinc-500">
            {rooms.length === 0
              ? 'Set up your first conference room to start recording with a fixed mic.'
              : 'Try clearing the search or selecting "all".'}
          </p>
          {rooms.length === 0 && (
            <button
              type="button"
              onClick={() => navigate('/rooms/new')}
              className="mt-4 inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500"
            >
              <Plus className="h-4 w-4" />
              Set up first room
            </button>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filteredRooms.map((room) => (
            <RoomCard
              key={room.id}
              room={room}
              sources={room.sources}
              busy={busyRoomId === room.id}
              onStart={() => handleStart(room)}
              onStop={() => handleStop(room)}
              onOpen={() => navigate(`/rooms/${room.raw_id || room.id}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
