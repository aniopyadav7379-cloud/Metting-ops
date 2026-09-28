import { useCallback, useEffect, useState } from 'react';
import {
  RefreshCw,
  Mic,
  AlertCircle,
  Check,
  Activity,
  Loader2,
  XCircle,
  CheckCircle2,
} from 'lucide-react';
import roomsApi, {
  type AudioDevice,
  type AudioDeviceProbe,
} from '../../services/roomsApi';

interface AudioDeviceListProps {
  orgSlug: string | null;
  selectedDevicePath?: string | null;
  selectedDevicePaths?: string[];
  onSelect?: (device: AudioDevice) => void;
  onSelectionChange?: (devices: AudioDevice[]) => void;
  selectionMode?: 'single' | 'multiple';
  /**
   * Optional className for the container. Defaults to a stack of cards.
   */
  className?: string;
  /**
   * Device paths that should render as already in-use / unavailable.
   */
  disabledDevicePaths?: string[];
  /**
   * Show the Test mic button per row. Default true. The setup wizard
   * embeds this list inside a small popover where the button is useful;
   * the room detail page's audio source list reuses this same component
   * but already has a separate test flow.
   */
  showTestButton?: boolean;
}

const PROBE_DURATION_SEC = 3.0;

/**
 * Maps a dBFS value (-90..0) to a 0..1 bar position. Anything quieter
 * than -60 is "silence" for the bar, anything above -3 is "clip".
 */
function dbToBarRatio(db: number): number {
  if (!Number.isFinite(db)) return 0;
  const clamped = Math.max(-60, Math.min(0, db));
  return (clamped + 60) / 60;
}

interface ProbeState {
  status: 'idle' | 'recording' | 'done' | 'error';
  result?: AudioDeviceProbe;
  error?: string;
  progressMs?: number;
}

/**
 * Fetches `/api/system/audio-devices` and renders each row as a
 * selectable card. Each row gets an optional "Test mic" button that
 * calls the backend probe endpoint and displays a 3-second VU bar +
 * RMS / peak readout.
 */
export default function AudioDeviceList({
  orgSlug,
  selectedDevicePath,
  selectedDevicePaths,
  onSelect,
  onSelectionChange,
  selectionMode = 'single',
  className = '',
  disabledDevicePaths = [],
  showTestButton = true,
}: AudioDeviceListProps) {
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Keyed by device_path so concurrent tests across rows don't stomp.
  const [probes, setProbes] = useState<Record<string, ProbeState>>({});

  const fetchDevices = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await roomsApi.listAudioDevices({ orgSlug });
      setDevices(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load audio devices');
      setDevices([]);
    } finally {
      setLoading(false);
    }
  }, [orgSlug]);

  useEffect(() => {
    fetchDevices();
  }, [fetchDevices]);

  const isMultiple = selectionMode === 'multiple';
  const selectedSet = new Set(selectedDevicePaths || []);
  const disabledSet = new Set(disabledDevicePaths);

  const startTest = useCallback(
    async (devicePath: string) => {
      // Tick a fake progress bar while the backend records. The backend
      // blocks for `PROBE_DURATION_SEC`, so we update locally so the user
      // sees motion. This is purely visual — the result still comes from
      // the server.
      setProbes((prev) => ({
        ...prev,
        [devicePath]: { status: 'recording', progressMs: 0 },
      }));
      const startedAt = Date.now();
      const totalMs = PROBE_DURATION_SEC * 1000;
      const ticker = window.setInterval(() => {
        const elapsed = Date.now() - startedAt;
        if (elapsed >= totalMs) {
          window.clearInterval(ticker);
          return;
        }
        setProbes((prev) => {
          const cur = prev[devicePath];
          if (!cur || cur.status !== 'recording') return prev;
          return {
            ...prev,
            [devicePath]: { ...cur, progressMs: elapsed },
          };
        });
      }, 100);

      try {
        const result = await roomsApi.probeAudioDevice(devicePath, {
          orgSlug,
          durationSec: PROBE_DURATION_SEC,
        });
        window.clearInterval(ticker);
        setProbes((prev) => ({
          ...prev,
          [devicePath]: { status: 'done', result },
        }));
      } catch (err) {
        window.clearInterval(ticker);
        setProbes((prev) => ({
          ...prev,
          [devicePath]: {
            status: 'error',
            error: err instanceof Error ? err.message : 'Probe failed',
          },
        }));
      }
    },
    [orgSlug],
  );

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      <div className="flex items-center justify-between">
        <div className="text-xs uppercase tracking-wide text-zinc-500">
          Server audio devices
        </div>
        <button
          type="button"
          onClick={fetchDevices}
          disabled={loading}
          className="flex items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {loading && <div className="text-sm text-zinc-500">Loading…</div>}

      {error && !loading && (
        <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle className="h-4 w-4" /> {error}
        </div>
      )}

      {!loading && !error && devices.length === 0 && (
        <div className="rounded-lg border border-dashed border-zinc-800 bg-zinc-950/50 px-3 py-4 text-center text-sm text-zinc-500">
          No audio devices detected on the server.
          <div className="mt-1 text-xs">
            Plug in a USB microphone and tap Refresh.
          </div>
        </div>
      )}

      {!loading && !error && devices.length > 0 && (
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {devices.map((d) => {
            const selected = isMultiple
              ? selectedSet.has(d.device_path)
              : selectedDevicePath === d.device_path;
            const disabled = disabledSet.has(d.device_path);
            const probe = probes[d.device_path];
            return (
              <div
                key={d.device_path}
                className={`flex flex-col gap-2 rounded-xl border px-3 py-3 transition-colors ${
                  selected
                    ? 'border-fuchsia-500/60 bg-fuchsia-500/10'
                    : disabled
                    ? 'border-zinc-800 bg-zinc-950/20 opacity-60'
                    : 'border-zinc-800 bg-zinc-950/40 hover:border-zinc-700 hover:bg-zinc-900'
                }`}
              >
                <button
                  type="button"
                  onClick={() => {
                    if (disabled) return;
                    if (isMultiple) {
                      if (!onSelectionChange) return;
                      const next = selected
                        ? (selectedDevicePaths || []).filter((path) => path !== d.device_path)
                        : [...(selectedDevicePaths || []), d.device_path];
                      onSelectionChange(devices.filter((device) => next.includes(device.device_path)));
                      return;
                    }
                    onSelect?.(d);
                  }}
                  className="flex items-start gap-3 text-left"
                  aria-pressed={selected}
                  disabled={disabled && !selected}
                >
                  <div className="mt-0.5">
                    {selected ? (
                      <Check className="h-4 w-4 text-fuchsia-300" />
                    ) : disabled ? (
                      <XCircle className="h-4 w-4 text-zinc-500" />
                    ) : (
                      <Mic className="h-4 w-4 text-zinc-400" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-zinc-100">
                      {d.device_name || d.card_name}
                    </div>
                    <div className="truncate text-xs text-zinc-500">
                      {d.card_name && d.card_name !== d.device_name
                        ? `${d.card_name}  ·  `
                        : ''}
                      <span className="font-mono">{d.device_path}</span>
                    </div>
                    {disabled && (
                      <div className="mt-1 text-[11px] uppercase tracking-wide text-zinc-500">
                        Already added to room
                      </div>
                    )}
                  </div>
                </button>

                {showTestButton && (
                  <ProbeRow
                    probe={probe}
                    onTest={() => startTest(d.device_path)}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProbeRow({
  probe,
  onTest,
}: {
  probe: ProbeState | undefined;
  onTest: () => void;
}) {
  if (!probe || probe.status === 'idle') {
    return (
      <button
        type="button"
        onClick={onTest}
        className="inline-flex w-fit items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
      >
        <Activity className="h-3 w-3" /> Test mic
      </button>
    );
  }

  if (probe.status === 'recording') {
    const totalMs = PROBE_DURATION_SEC * 1000;
    const pct = Math.min(100, ((probe.progressMs ?? 0) / totalMs) * 100);
    return (
      <div className="flex flex-col gap-1.5">
        <div className="inline-flex w-fit items-center gap-1.5 text-xs text-zinc-300">
          <Loader2 className="h-3 w-3 animate-spin" /> Recording 3 s…
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
          <div
            className="h-full bg-gradient-to-r from-fuchsia-500 via-violet-400 to-sky-400 transition-[width] duration-100"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    );
  }

  if (probe.status === 'error') {
    return (
      <div className="flex flex-col gap-1.5">
        <div className="inline-flex items-center gap-1.5 text-xs text-red-300">
          <XCircle className="h-3.5 w-3.5" />
          <span className="truncate" title={probe.error}>
            {probe.error || 'Test failed'}
          </span>
        </div>
        <button
          type="button"
          onClick={onTest}
          className="inline-flex w-fit items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
        >
          <Activity className="h-3 w-3" /> Retry
        </button>
      </div>
    );
  }

  // status === 'done'
  const result = probe.result;
  if (!result) return null;
  const rmsRatio = dbToBarRatio(result.rms_db);
  const peakRatio = dbToBarRatio(result.peak_db);

  return (
    <div className="flex flex-col gap-1.5">
      <div
        className={`inline-flex items-center gap-1.5 text-xs ${
          result.detected_audio ? 'text-emerald-300' : 'text-amber-300'
        }`}
      >
        {result.detected_audio ? (
          <>
            <CheckCircle2 className="h-3.5 w-3.5" />
            Mic detected · peak {result.peak_db.toFixed(0)} dB, RMS{' '}
            {result.rms_db.toFixed(0)} dB
          </>
        ) : (
          <>
            <AlertCircle className="h-3.5 w-3.5" />
            No audio detected (peak {result.peak_db.toFixed(0)} dB) — speak
            closer or check cable
          </>
        )}
      </div>
      <div
        className="relative h-2 overflow-hidden rounded-full bg-zinc-800"
        role="meter"
        aria-label="Probed mic level"
        aria-valuenow={Math.round(rmsRatio * 100)}
      >
        <div
          className="absolute inset-y-0 left-0 bg-gradient-to-r from-emerald-500 via-emerald-400 to-amber-300"
          style={{ width: `${Math.round(rmsRatio * 100)}%` }}
        />
        <div
          className="absolute inset-y-0 w-0.5 bg-amber-200"
          style={{ left: `${Math.round(peakRatio * 100)}%` }}
          title={`Peak ${result.peak_db.toFixed(1)} dB`}
        />
      </div>
      <button
        type="button"
        onClick={onTest}
        className="inline-flex w-fit items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
      >
        <Activity className="h-3 w-3" /> Test again
      </button>
    </div>
  );
}
