import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Milestone, RefreshCw } from 'lucide-react';
import { config } from '../config';

interface Moment {
  id: number;
  session_id: string;
  session_title: string;
  moment_type: string;
  description: string;
  quote: string | null;
  timestamp: number | null;
  speaker: string | null;
}

const MOMENT_TYPES = [
  '',
  'decision',
  'action_item',
  'explanation',
  'question',
  'topic_transition',
  'disagreement',
  'conclusion',
  'key_concept',
];

function formatTimestamp(seconds: number | null): string {
  if (seconds === null) return '';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export default function ImportantMoments() {
  const [moments, setMoments] = useState<Moment[]>([]);
  const [typeFilter, setTypeFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const qs = typeFilter ? `?moment_type=${encodeURIComponent(typeFilter)}` : '';
    fetch(`${config.apiBaseUrl}/api/moments${qs}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setMoments(data);
      })
      .catch((err) => {
        if (!cancelled) setError(`Failed to load: ${(err as Error).message}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [typeFilter]);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-fuchsia-200">
            <Milestone className="h-5 w-5" aria-hidden="true" />
            <span className="text-sm font-medium">Timeline</span>
          </div>
          <h1 className="mt-2 text-2xl font-semibold text-white">Important moments</h1>
          <p className="mt-1 max-w-2xl text-sm text-zinc-400">
            Decisions, explanations, questions, and turning points across your meetings and lectures, each
            linked to the moment it happened — when a moment's exact wording can't be located in the
            transcript, no timestamp is shown rather than a guessed one.
          </p>
        </div>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          aria-label="Filter by moment type"
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-fuchsia-500"
        >
          {MOMENT_TYPES.map((t) => (
            <option key={t} value={t}>
              {t ? t.replace('_', ' ') : 'All types'}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-500/30 bg-red-950/30 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center gap-2 px-2 py-6 text-sm text-zinc-400">
          <RefreshCw className="h-4 w-4 animate-spin" />
          Loading...
        </div>
      ) : moments.length === 0 ? (
        <div className="rounded-lg border border-zinc-800 px-4 py-8 text-sm text-zinc-500">
          No important moments extracted yet.
        </div>
      ) : (
        <ul className="space-y-2">
          {moments.map((moment) => (
            <li key={moment.id} className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-full border border-fuchsia-500/30 bg-fuchsia-950/40 px-2 py-0.5 text-[10px] uppercase tracking-wide text-fuchsia-300">
                  {moment.moment_type.replace('_', ' ')}
                </span>
                {moment.timestamp !== null && (
                  <span className="font-mono text-xs text-zinc-500">{formatTimestamp(moment.timestamp)}</span>
                )}
                {moment.speaker && <span className="text-xs text-zinc-500">· {moment.speaker}</span>}
              </div>
              <p className="mt-1 text-sm text-zinc-100">{moment.description}</p>
              <Link to={`/sessions/${moment.session_id}`} className="mt-1 inline-block text-xs text-fuchsia-300 hover:underline">
                {moment.session_title || 'Untitled meeting'}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
