import { useEffect, useRef, useState } from 'react';
import { Activity, AlertCircle, Mic, MicOff } from 'lucide-react';
import roomsApi, { type RoomLevelFrame } from '../../services/roomsApi';

interface RoomLevelMeterProps {
  /** API id of the room (numeric or UUID string). */
  roomId: number | string;
  /** Active org slug for the X-MeetingOps-Org cookie path. */
  orgSlug: string | null;
  /**
   * Whether the room is recording. The component subscribes to the SSE
   * stream only when this is true — otherwise it renders a "Not
   * recording" placeholder so we don't burn a worker on idle rooms.
   */
  recording: boolean;
  /**
   * Label shown above the bar. Defaults to "Live level".
   */
  label?: string;
}

/**
 * Maps a dBFS value (-90..0) to a 0..1 bar position with the same curve
 * used by AudioDeviceList. Anything <-60 dB is treated as silent.
 */
function dbToBarRatio(db: number): number {
  if (!Number.isFinite(db)) return 0;
  const clamped = Math.max(-60, Math.min(0, db));
  return (clamped + 60) / 60;
}

/**
 * Subscribes to `/api/rooms/{id}/levels` (SSE) and animates a horizontal
 * VU bar with peak markers. Falls back to silence + a "no data" hint
 * when the recorder isn't producing frames.
 *
 * The stream is opened on mount when `recording` is true and torn down
 * on unmount or when `recording` flips false. EventSource auto-
 * reconnects, but we close+reopen on stream-end to avoid stale state.
 */
export default function RoomLevelMeter({
  roomId,
  orgSlug,
  recording,
  label = 'Live level',
}: RoomLevelMeterProps) {
  const [frame, setFrame] = useState<RoomLevelFrame | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!recording) {
      // Close any leftover stream and reset to placeholder state.
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      setFrame(null);
      setError(null);
      setActive(false);
      return;
    }

    const es = roomsApi.openRoomLevelStream(roomId, { orgSlug });
    esRef.current = es;

    es.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data) as RoomLevelFrame & {
          active?: boolean;
        };
        setFrame(data);
        setActive(!!data.active);
        setError(null);
      } catch {
        // Ignore malformed frames — the next one will likely be fine.
      }
    };
    es.addEventListener('end', () => {
      setActive(false);
      es.close();
      esRef.current = null;
    });
    es.onerror = () => {
      // EventSource auto-reconnects, but surface a hint so a perma-
      // disconnect (auth fail, network drop) is visible.
      setError('Stream interrupted, retrying…');
    };

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [roomId, orgSlug, recording]);

  if (!recording) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-black/30 px-3 py-2 text-xs text-zinc-500">
        <MicOff className="h-3.5 w-3.5" /> {label}: not recording
      </div>
    );
  }

  const rmsDb = frame?.rms_db ?? -90;
  const peakDb = frame?.peak_db ?? -90;
  const rmsRatio = dbToBarRatio(rmsDb);
  const peakRatio = dbToBarRatio(peakDb);

  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-zinc-800 bg-black/30 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <div className="inline-flex items-center gap-1.5 text-xs text-zinc-300">
          <Activity className="h-3.5 w-3.5 text-emerald-300" /> {label}
        </div>
        <div className="font-mono text-[11px] tabular-nums text-zinc-400">
          {frame ? (
            <>
              RMS {rmsDb.toFixed(0)} dB · Peak {peakDb.toFixed(0)} dB
            </>
          ) : (
            <span className="text-zinc-600">connecting…</span>
          )}
        </div>
      </div>
      <div
        className="relative h-2.5 overflow-hidden rounded-full bg-zinc-800"
        role="meter"
        aria-label={label}
        aria-valuenow={Math.round(rmsRatio * 100)}
      >
        <div
          className="absolute inset-y-0 left-0 bg-gradient-to-r from-emerald-500 via-emerald-400 to-amber-300 transition-[width] duration-75"
          style={{ width: `${Math.round(rmsRatio * 100)}%` }}
        />
        <div
          className="absolute inset-y-0 w-0.5 bg-amber-200"
          style={{ left: `${Math.round(peakRatio * 100)}%` }}
          title={`Peak ${peakDb.toFixed(1)} dB`}
        />
      </div>
      {!active && !error && frame && (
        <div className="inline-flex items-center gap-1 text-[11px] text-amber-300">
          <Mic className="h-3 w-3" /> Recorder warming up…
        </div>
      )}
      {error && (
        <div className="inline-flex items-center gap-1 text-[11px] text-amber-300">
          <AlertCircle className="h-3 w-3" /> {error}
        </div>
      )}
    </div>
  );
}
