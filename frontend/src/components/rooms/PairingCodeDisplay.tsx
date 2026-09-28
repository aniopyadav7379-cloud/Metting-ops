import { useEffect, useMemo, useState } from 'react';
import { Copy, RefreshCw, Check, Clock } from 'lucide-react';
import type { PairingCode } from '../../services/roomsApi';

interface PairingCodeDisplayProps {
  code: PairingCode | null;
  /** Called when admin presses Regenerate or when no code yet. */
  onGenerate: () => Promise<void>;
  loading?: boolean;
}

function formatRemaining(ms: number): string {
  if (ms <= 0) return 'Expired';
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${min}:${sec.toString().padStart(2, '0')}`;
}

/**
 * Big-format pairing code display. Splits the 6-digit code 3+3 with a
 * generous gap so it's readable across the room when a tech is poking
 * it into a satellite. Countdown ticks every second; on expiry the
 * regenerate button is auto-emphasised.
 */
export default function PairingCodeDisplay({
  code,
  onGenerate,
  loading,
}: PairingCodeDisplayProps) {
  const [, setTick] = useState(0);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!code) return;
    const i = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(i);
  }, [code]);

  const remainingMs = useMemo(() => {
    if (!code) return 0;
    return new Date(code.expires_at).getTime() - Date.now();
  }, [code, /* tick */ setTick]);

  const expired = !!code && remainingMs <= 0;

  const display = code ? `${code.code.slice(0, 3)} ${code.code.slice(3)}` : null;

  const copy = async () => {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code.code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore clipboard failures */
    }
  };

  if (!code) {
    return (
      <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <div className="text-sm text-zinc-400">
          No active pairing code. Generate a 6-digit code to bind a Room device.
        </div>
        <button
          type="button"
          onClick={onGenerate}
          disabled={loading}
          className="mt-3 inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Generate pairing code
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
      <div className="text-xs uppercase tracking-wide text-zinc-500">
        Pairing code
      </div>
      <div className="mt-2 flex items-center justify-between gap-3 flex-wrap">
        <div
          className={`font-mono tabular-nums text-5xl font-semibold tracking-wider ${
            expired ? 'text-zinc-600 line-through' : 'text-white'
          }`}
        >
          {display}
        </div>
        <div className="flex flex-col items-end gap-1">
          <div
            className={`flex items-center gap-1 text-sm ${
              expired ? 'text-red-300' : remainingMs < 60_000 ? 'text-amber-300' : 'text-zinc-300'
            }`}
          >
            <Clock className="h-3.5 w-3.5" />
            {expired ? 'Expired' : `Expires in ${formatRemaining(remainingMs)}`}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={copy}
              disabled={expired}
              className="inline-flex items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
            >
              {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
              {copied ? 'Copied' : 'Copy'}
            </button>
            <button
              type="button"
              onClick={onGenerate}
              disabled={loading}
              className="inline-flex items-center gap-1 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:from-fuchsia-500 hover:to-indigo-500 disabled:opacity-50"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
              {expired ? 'Regenerate' : 'New code'}
            </button>
          </div>
        </div>
      </div>
      <div className="mt-3 text-xs text-zinc-500">
        Enter this code on your room device to bind it to this room. Codes
        expire after 10 minutes and can only be redeemed once.
      </div>
    </div>
  );
}
