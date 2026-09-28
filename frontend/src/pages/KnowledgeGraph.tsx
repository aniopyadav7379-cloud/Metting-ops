import React, { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Network, Search, RefreshCw } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';
import type { BrigadeGraphResponse } from '../components/BrigadeGraphViewer';

// The graph page itself is route-lazy. Keep the WebGL renderer lazy too so
// opening the picker or an empty graph response never downloads Three.js.
const BrigadeGraphViewer = lazy(() => import('../components/BrigadeGraphViewer'));

function GraphLoadingFallback() {
  return (
    <div className="flex h-[520px] items-center justify-center rounded-lg border border-zinc-800 bg-black/20 text-sm text-zinc-400">
      Loading interactive graph…
    </div>
  );
}

interface SpeakerRow {
  id: number;
  display_name: string;
  email?: string | null;
  company?: string | null;
  contact_link_confirmed?: boolean;
}

/**
 * Cross-meeting Knowledge Graph page (person-centric). Picks a person from
 * the org's speakers and renders their Brigade subgraph (meetings, co-speakers,
 * topics, decisions, action items) via the shared 3D viewer. The page owns the
 * fetch + empty-state copy; the viewer only renders the supplied data.
 *
 * Shipped behind VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED (see AppRouterSimplified).
 */
export default function KnowledgeGraph() {
  const { getOrgQueryUrl } = useOrg();

  const [speakers, setSpeakers] = useState<SpeakerRow[]>([]);
  const [speakersError, setSpeakersError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<SpeakerRow | null>(null);
  const [hops, setHops] = useState<1 | 2>(2);
  const [reloadKey, setReloadKey] = useState(0);

  const [graph, setGraph] = useState<BrigadeGraphResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-load on mount: consult /me once, then (if no self-speaker) feature
  // the first person — so the page opens on a real graph, not a blank canvas.
  const [meResolved, setMeResolved] = useState(false);
  const [autoSelected, setAutoSelected] = useState(false);

  // Walk-the-graph history (person → person via node clicks) → powers Back.
  const [history, setHistory] = useState<SpeakerRow[]>([]);

  const authFetch = useCallback(
    async (path: string) => {
      const token = localStorage.getItem('access_token');
      const url = getOrgQueryUrl(`${config.apiUrl}${path}`);
      const resp = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
      }
      return resp.json();
    },
    [getOrgQueryUrl],
  );

  // Load the org's people for the picker.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows: SpeakerRow[] = await authFetch('/api/speakers');
        if (!cancelled) setSpeakers(Array.isArray(rows) ? rows : []);
      } catch (e) {
        if (!cancelled) {
          setSpeakersError(e instanceof Error ? e.message : 'Failed to load people');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authFetch]);

  // Fetch the selected person's graph when person / hops / retry changes.
  useEffect(() => {
    if (!selected) {
      setGraph(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const body: BrigadeGraphResponse = await authFetch(
          `/api/knowledge-graph/person/${selected.id}?hops=${hops}`,
        );
        if (!cancelled) setGraph(body);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load graph');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected, hops, reloadKey, authFetch]);

  // Default-load step 1: consult /me once. If the viewer has a speaker
  // (linked_user_id / email / name match), open straight onto their graph.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me: BrigadeGraphResponse = await authFetch(
          '/api/knowledge-graph/person/me?hops=2',
        );
        if (!cancelled && me && me.speaker_id) {
          setSelected({ id: me.speaker_id, display_name: me.speaker_name || 'You' });
          setAutoSelected(true);
        }
      } catch {
        /* fall through to the featured-person fallback */
      } finally {
        if (!cancelled) setMeResolved(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authFetch]);

  // Default-load step 2: no self-speaker -> feature the first person once the
  // people list has loaded, so the page is never blank when data exists.
  useEffect(() => {
    if (autoSelected || selected || !meResolved) return;
    if (speakers.length > 0) {
      setSelected(speakers[0]);
      setAutoSelected(true);
    }
  }, [autoSelected, selected, meResolved, speakers]);

  const filtered = useMemo(() => {
    const sorted = [...speakers].sort((a, b) =>
      a.display_name.localeCompare(b.display_name),
    );
    const q = query.trim().toLowerCase();
    const matches = q
      ? sorted.filter(
          (s) =>
            s.display_name.toLowerCase().includes(q) ||
            (s.company ?? '').toLowerCase().includes(q),
        )
      : sorted;
    return matches.slice(0, 50);
  }, [speakers, query]);

  const nodes = graph?.nodes ?? [];
  const showViewer =
    !!graph &&
    nodes.length > 0 &&
    graph.reason !== 'not_synced_yet' &&
    graph.reason !== 'live_failed';

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <div className="mb-5 flex items-center gap-3">
        <Network className="h-6 w-6 text-fuchsia-400" />
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Knowledge Graph</h1>
          <p className="text-sm text-zinc-400">
            Explore how a person connects across your meetings — their meetings,
            co-speakers, topics, decisions, and action items.
          </p>
        </div>
      </div>

      {/* Controls */}
      <div className="mb-4 grid gap-3 sm:grid-cols-[1fr_auto]">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
          <input
            value={selected ? selected.display_name : query}
            onChange={(e) => {
              setSelected(null);
              setQuery(e.target.value);
            }}
            placeholder="Search people…"
            className="w-full rounded-lg border border-zinc-800 bg-zinc-900 py-2 pl-9 pr-3 text-sm text-zinc-100 outline-none focus:border-fuchsia-500"
          />
          {!selected && query.trim() !== '' && filtered.length > 0 && (
            <div className="absolute z-10 mt-1 max-h-72 w-full overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-900 shadow-xl">
              {filtered.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => {
                    setHistory([]);
                    setSelected(s);
                    setQuery('');
                  }}
                  className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm text-zinc-200 hover:bg-zinc-800"
                >
                  <span className="truncate">{s.display_name}</span>
                  {s.company ? (
                    <span className="truncate text-xs text-zinc-500">{s.company}</span>
                  ) : null}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="inline-flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900 p-1">
          {[1, 2].map((h) => (
            <button
              key={h}
              type="button"
              onClick={() => setHops(h as 1 | 2)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                hops === h
                  ? 'bg-fuchsia-600/30 text-fuchsia-200'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {h} hop{h > 1 ? 's' : ''}
            </button>
          ))}
        </div>
      </div>

      {speakersError && (
        <p className="mb-3 text-sm text-amber-400">Couldn’t load people: {speakersError}</p>
      )}

      {history.length > 0 && (
        <button
          type="button"
          onClick={() => {
            const prev = history[history.length - 1];
            setHistory((h) => h.slice(0, -1));
            if (prev) setSelected(prev);
          }}
          className="mb-3 inline-flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-300 transition-colors hover:border-fuchsia-500 hover:text-fuchsia-200"
        >
          ← Back to {history[history.length - 1].display_name}
        </button>
      )}

      {/* Body */}
      {!selected && !meResolved && (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-zinc-800 bg-black/20 px-6 py-16 text-sm text-zinc-400">
          <RefreshCw className="h-4 w-4 animate-spin" /> Opening your knowledge graph…
        </div>
      )}

      {!selected && meResolved && (
        <div className="rounded-lg border border-dashed border-zinc-800 bg-black/20 px-6 py-16 text-center text-sm text-zinc-400">
          {speakers.length > 0
            ? 'Pick a person above to see their meeting graph.'
            : 'No people yet — record and reprocess a few meetings to build your graph.'}
        </div>
      )}

      {selected && loading && (
        <div className="flex items-center justify-center gap-2 rounded-lg border border-zinc-800 bg-black/20 px-6 py-16 text-sm text-zinc-400">
          <RefreshCw className="h-4 w-4 animate-spin" /> Building {selected.display_name}’s
          graph…
        </div>
      )}

      {selected && !loading && error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/5 px-6 py-12 text-center text-sm text-red-300">
          {error}
        </div>
      )}

      {selected && !loading && !error && graph && (
        <div className="space-y-3">
          {graph.reason === 'not_synced_yet' && (
            <div className="rounded-lg border border-zinc-800 bg-black/20 px-6 py-12 text-center text-sm text-zinc-400">
              No graph yet for{' '}
              <span className="text-zinc-200">{selected.display_name}</span>. Record and
              reprocess a couple of meetings with them to build it.
            </div>
          )}
          {graph.reason === 'live_failed' && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-6 py-12 text-center text-sm text-amber-300">
              Couldn’t reach the graph service.{' '}
              <button
                type="button"
                onClick={() => setReloadKey((k) => k + 1)}
                className="underline hover:text-amber-200"
              >
                Retry
              </button>
              .
            </div>
          )}
          {graph.reason === 'single_meeting' && (
            <p className="text-xs text-zinc-500">
              Only one meeting so far with {selected.display_name} — the graph grows as you
              record more meetings with shared attendees.
            </p>
          )}
          {graph.truncated && (
            <p className="text-xs text-amber-400">
              Showing the first {graph.node_cap ?? nodes.length} nodes — narrow by
              person or reduce hops.
            </p>
          )}

          {showViewer && (
            <Suspense fallback={<GraphLoadingFallback />}>
            <BrigadeGraphViewer
              data={graph}
              height={520}
              onSelectSpeaker={(id, name) => {
                setHistory((h) => (selected ? [...h, selected] : h));
                setSelected({ id, display_name: name });
              }}
            />
            </Suspense>
          )}
        </div>
      )}
    </div>
  );
}
