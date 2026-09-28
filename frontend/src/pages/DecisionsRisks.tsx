import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Scale, ShieldAlert, RefreshCw } from 'lucide-react';
import { config } from '../config';

interface SourcedInsightItem {
  text: string;
  session_id: string;
  session_title: string;
  session_date: string | null;
}

type Tab = 'decisions' | 'risks';

function formatDate(value: string | null): string {
  if (!value) return '';
  try {
    return new Date(value).toLocaleDateString();
  } catch {
    return value;
  }
}

export default function DecisionsRisks() {
  const [tab, setTab] = useState<Tab>('decisions');
  const [items, setItems] = useState<SourcedInsightItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const endpoint = tab === 'decisions' ? '/api/decisions' : '/api/risks';
    fetch(`${config.apiBaseUrl}${endpoint}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setItems(data);
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
  }, [tab]);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-fuchsia-200">
            {tab === 'decisions' ? (
              <Scale className="h-5 w-5" aria-hidden="true" />
            ) : (
              <ShieldAlert className="h-5 w-5" aria-hidden="true" />
            )}
            <span className="text-sm font-medium">Meeting outcomes</span>
          </div>
          <h1 className="mt-2 text-2xl font-semibold text-white">Decisions &amp; risks</h1>
          <p className="mt-1 max-w-2xl text-sm text-zinc-400">
            Every decision, risk, and open question extracted across your meetings, each linked back to
            the meeting it came from — nothing here is invented.
          </p>
        </div>
        <Link
          to="/sessions"
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 transition hover:border-fuchsia-500/50 hover:text-white"
        >
          View all meetings
        </Link>
      </div>

      <div className="mb-4 flex gap-1 border-b border-zinc-800" role="tablist" aria-label="Decisions or risks">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'decisions'}
          onClick={() => setTab('decisions')}
          className={`-mb-px rounded-t-md border-b-2 px-4 py-2 text-sm font-medium transition ${
            tab === 'decisions'
              ? 'border-fuchsia-500 text-fuchsia-200'
              : 'border-transparent text-zinc-400 hover:text-zinc-200'
          }`}
        >
          Decisions
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'risks'}
          onClick={() => setTab('risks')}
          className={`-mb-px rounded-t-md border-b-2 px-4 py-2 text-sm font-medium transition ${
            tab === 'risks'
              ? 'border-fuchsia-500 text-fuchsia-200'
              : 'border-transparent text-zinc-400 hover:text-zinc-200'
          }`}
        >
          Risks &amp; open questions
        </button>
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
      ) : items.length === 0 ? (
        <div className="rounded-lg border border-zinc-800 px-4 py-8 text-sm text-zinc-500">
          {tab === 'decisions'
            ? 'No decisions have been extracted yet.'
            : 'No risks or open questions have been extracted yet.'}
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((item, idx) => (
            <li
              key={`${item.session_id}-${idx}`}
              className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-4 py-3"
            >
              <p className="text-sm text-zinc-100">{item.text}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                <Link
                  to={`/sessions/${item.session_id}`}
                  className="text-fuchsia-300 hover:underline"
                >
                  {item.session_title || 'Untitled meeting'}
                </Link>
                {item.session_date && <span>· {formatDate(item.session_date)}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
