import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Circle,
  Clock,
  MapPin,
  Mic,
  AlertTriangle,
  PowerOff,
  Square,
  Play,
} from 'lucide-react';
import type { Room, RoomSource } from '../../services/roomsApi';

interface RoomCardProps {
  room: Room;
  sources?: RoomSource[];
  onStart: () => void;
  onStop: () => void;
  onOpen: () => void;
  /** Disabled while a start/stop is in-flight. */
  busy?: boolean;
}

function formatElapsed(startIso: string): string {
  const start = new Date(startIso).getTime();
  const sec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const hrs = Math.floor(sec / 3600);
  const mins = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function relativeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const sec = Math.max(0, Math.floor(diffMs / 1000));
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  return days === 1 ? '1 day ago' : `${days} days ago`;
}

function summarizeSources(sources?: RoomSource[]): string {
  if (!sources || sources.length === 0) return 'No audio source configured';
  const active = sources.find((s) => s.status === 'recording') || sources[0];
  const others = sources.filter((s) => s.id !== active.id);
  const main = active.label || `${active.hardware_type}: ${active.device_path || active.device_id || ''}`;
  if (others.length === 0) return main;
  return `${main} + ${others.length} more`;
}

export default function RoomCard({
  room,
  sources,
  onStart,
  onStop,
  onOpen,
  busy,
}: RoomCardProps) {
  // 1Hz elapsed tick while recording so the card UI stays alive.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (room.status !== 'recording') return;
    const i = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(i);
  }, [room.status]);

  const navigate = useNavigate();

  let border = 'border-zinc-800';
  let StatusIcon = Circle;
  let statusColor = 'text-zinc-400';
  let statusLabel: string;

  if (room.status === 'recording') {
    border = 'border-emerald-500/50';
    StatusIcon = Circle;
    statusColor = 'text-emerald-400 fill-emerald-400 animate-pulse';
    statusLabel = room.current_session_started_at
      ? `Recording  ·  ${formatElapsed(room.current_session_started_at)} elapsed`
      : 'Recording';
  } else if (room.status === 'error') {
    border = 'border-red-500/50';
    StatusIcon = AlertTriangle;
    statusColor = 'text-red-400';
    statusLabel = 'Error  ·  Check audio source';
  } else if (room.status === 'offline') {
    border = 'border-amber-500/40';
    StatusIcon = PowerOff;
    statusColor = 'text-amber-300';
    statusLabel = 'Offline';
  } else {
    statusLabel = room.last_recording_at
      ? `Idle  ·  Last session ${relativeAgo(room.last_recording_at)}`
      : 'Idle  ·  No recordings yet';
  }

  const isRecording = room.status === 'recording';

  return (
    <div
      className={`group flex flex-col gap-3 rounded-2xl border ${border} bg-zinc-950/60 p-5 shadow-lg shadow-black/30 transition-colors hover:bg-zinc-900/60`}
    >
      <div className="flex items-start justify-between gap-2">
        <button
          type="button"
          onClick={onOpen}
          className="flex-1 min-w-0 text-left"
        >
          <h3 className="truncate text-base font-semibold text-zinc-100 group-hover:text-white">
            {room.name}
          </h3>
          {room.location && (
            <div className="mt-1 flex items-center gap-1 text-xs text-zinc-500">
              <MapPin className="h-3 w-3" /> {room.location}
            </div>
          )}
        </button>
      </div>

      <div className="flex items-center gap-2 text-sm">
        <StatusIcon className={`h-3 w-3 ${statusColor}`} />
        <span className="text-zinc-300 font-mono tabular-nums">{statusLabel}</span>
      </div>

      <div className="flex items-center gap-2 text-xs text-zinc-400">
        <Mic className="h-3.5 w-3.5" />
        <span className="truncate">{summarizeSources(sources)}</span>
      </div>

      <div className="mt-1 flex items-center gap-2 pt-2 border-t border-zinc-800/80">
        <button
          type="button"
          onClick={() => {
            onOpen();
            navigate(`/rooms/${room.raw_id || room.id}`);
          }}
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-200 hover:bg-zinc-800"
        >
          View
        </button>
        {isRecording ? (
          <button
            type="button"
            disabled={busy}
            onClick={onStop}
            className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50 flex items-center gap-1"
          >
            <Square className="h-3 w-3" /> Stop
          </button>
        ) : (
          <button
            type="button"
            disabled={busy || room.status === 'offline' || room.status === 'error'}
            onClick={onStart}
            className="rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50 flex items-center gap-1"
          >
            <Play className="h-3 w-3" /> Start
          </button>
        )}
        {room.legal_hold && (
          <span
            className="ml-auto rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-amber-200"
            title="Legal hold is on — recordings cannot be deleted"
          >
            Legal hold
          </span>
        )}
        {!room.legal_hold && (
          <span className="ml-auto flex items-center gap-1 text-[11px] text-zinc-500">
            <Clock className="h-3 w-3" />
            {room.retention_days ? `${room.retention_days}d retention` : 'No retention limit'}
          </span>
        )}
      </div>
    </div>
  );
}
