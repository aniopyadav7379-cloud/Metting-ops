/**
 * Brigade Phase 2 — in-page 3D graph viewer for SessionDetails.
 *
 * Renders the current meeting's Brigade nodes + 1-hop neighbors
 * (Speaker / ActionItem / Topic / Decision) as a force-directed 3D
 * graph using react-force-graph-3d (which wraps three.js under the
 * hood — ~500KB+ footprint, code-split via React.lazy in
 * SessionDetails so this only loads when the user expands the
 * Knowledge graph section).
 *
 * Data source: GET /api/sessions/{id}/brigade-graph, which queries
 * Brigade's /api/v1/knowledge/context/{name} endpoint and maps the
 * response to the {nodes, links} shape react-force-graph-3d
 * consumes. Server caches the result for 30s.
 *
 * Three rendering states:
 *   - "synced" (nodes + links populated): renders the 3D graph.
 *   - "not_synced_yet" (empty + reason='not_synced_yet'): shows the
 *     friendly "this meeting hasn't been synced yet" message. This
 *     is the expected state for new sessions and any deployment
 *     where BRIGADE_API_KEY isn't configured.
 *   - "live_failed" (empty + reason='live_failed'): synced once but
 *     Brigade is currently unreachable; shows a retry button.
 *
 * The component is internally headless on data — it owns its fetch
 * + state machine — so SessionDetails just passes sessionId and the
 * viewer handles the rest.
 */
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import ForceGraph3D from 'react-force-graph-3d';
import { ExternalLink, RefreshCw } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';

// ---------------------------------------------------------------------------
// Wire types — mirror the backend response in api/recording.py
// (get_brigade_graph). Keep these here rather than a shared types file
// so the BrigadeGraphViewer module is self-contained for the
// React.lazy code-split.
// ---------------------------------------------------------------------------

export interface BrigadeNode {
  id: string;
  label: string; // "Meeting" | "Speaker" | "ActionItem" | "Topic" | "Decision" | other
  name: string;
  is_focus?: boolean;
  properties?: Record<string, unknown>;
}

export interface BrigadeLink {
  source: string;
  target: string;
  type: string; // "HAS_SPEAKER" | "HAS_ACTION_ITEM" | "HAS_TOPIC" | "HAS_DECISION" | "ASSIGNED_TO" | "DECIDED_BY" | other
  properties?: Record<string, unknown>;
}

export interface BrigadeGraphResponse {
  nodes: BrigadeNode[];
  links: BrigadeLink[];
  graph_url: string | null;
  focus: string | null;
  reason: 'not_synced_yet' | 'live_failed' | 'single_meeting' | null;
  synced_at?: string | null;
  // Knowledge Graph person-view extras (optional; unused by the session view).
  truncated?: boolean;
  node_cap?: number;
  speaker_id?: number;
  speaker_name?: string;
}

// ---------------------------------------------------------------------------
// Color + size maps per label. Indigo/green/orange/blue/purple per the
// spec; "other" labels (Concept, Person, etc) fall back to slate.
// ---------------------------------------------------------------------------

const LABEL_COLORS: Record<string, string> = {
  Meeting: '#6366f1', // indigo-500 — focus node
  Speaker: '#10b981', // emerald-500 — people
  ActionItem: '#f97316', // orange-500 — things to do
  Topic: '#3b82f6', // blue-500 — discussion threads
  Decision: '#a855f7', // purple-500 — outcomes
};

const LABEL_FALLBACK_COLOR = '#64748b'; // slate-500

function colorForLabel(label: string): string {
  return LABEL_COLORS[label] || LABEL_FALLBACK_COLOR;
}

function sizeForNode(node: BrigadeNode): number {
  // The focus person is the biggest so the user orients at a glance;
  // meetings (the hubs) next, everything else small.
  if (node.is_focus) return 12;
  if (node.label === 'Meeting') return 7;
  return 4;
}

// ---------------------------------------------------------------------------
// Avatar + per-type node detail. The graph nodes are hydrated server-side
// (services/kg_hydrate.py) so `properties.kind` discriminates the shape and
// carries human content (titles, text, dates, deep-link ids, avatar_url).
// ---------------------------------------------------------------------------

function initialsOf(name: string): string {
  const parts = (name || '').split(/\s+/).filter(Boolean);
  const ini = parts.map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  return ini || '?';
}

const Avatar: React.FC<{ name: string; url?: string | null; size?: number }> = ({
  name,
  url,
  size = 36,
}) => {
  const [broken, setBroken] = useState(false);
  if (url && !broken) {
    return (
      <img
        src={url}
        alt={name}
        onError={() => setBroken(true)}
        className="rounded-full object-cover ring-2 ring-white/70 shadow"
        style={{ width: size, height: size }}
      />
    );
  }
  return (
    <div
      className="flex items-center justify-center rounded-full font-semibold text-white shadow ring-2 ring-white/70"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.4,
        background: 'linear-gradient(135deg,#a855f7,#6366f1)',
      }}
    >
      {initialsOf(name)}
    </div>
  );
};

const DetailRow: React.FC<{ label: string; children: React.ReactNode }> = ({
  label,
  children,
}) => (
  <div className="flex gap-2 text-xs">
    <span className="w-20 shrink-0 text-slate-400">{label}</span>
    <span className="break-words text-slate-700">{children}</span>
  </div>
);

function fmtDate(v: unknown): string | null {
  if (typeof v !== 'string') return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime())
    ? null
    : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function titleCase(s: unknown): string {
  return String(s ?? '').replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

const NodeDetailBody: React.FC<{
  node: BrigadeNode;
  onSelectSpeaker?: (speakerId: number, name: string) => void;
}> = ({ node, onSelectSpeaker }) => {
  const p = (node.properties || {}) as Record<string, any>;
  const kind = p.kind as string | undefined;

  if (kind === 'speaker') {
    const meta = [p.role, p.company].filter(Boolean).join(' · ');
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-3">
          <Avatar name={node.name} url={p.avatar_url} size={44} />
          <div className="min-w-0">
            <div className="truncate font-semibold text-slate-900">{node.name}</div>
            {meta && <div className="truncate text-xs text-slate-500">{meta}</div>}
          </div>
        </div>
        {p.email && <DetailRow label="Email">{p.email}</DetailRow>}
        {!node.is_focus && onSelectSpeaker && typeof p.speaker_id === 'number' && (
          <button
            type="button"
            onClick={() => onSelectSpeaker(p.speaker_id, node.name)}
            className="mt-1 inline-flex items-center gap-1 rounded-md bg-fuchsia-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-fuchsia-700"
          >
            View {node.name.split(' ')[0]}’s graph →
          </button>
        )}
        {node.is_focus && (
          <p className="text-xs text-slate-400">The person at the center of this graph.</p>
        )}
      </div>
    );
  }

  if (kind === 'meeting') {
    const date = fmtDate(p.date);
    return (
      <div className="space-y-1.5">
        <div className="font-semibold text-slate-900">{node.name}</div>
        {date && <DetailRow label="When">{date}</DetailRow>}
        {p.duration_min != null && <DetailRow label="Length">{p.duration_min} min</DetailRow>}
        {p.status && <DetailRow label="Status">{titleCase(p.status)}</DetailRow>}
        {p.session_id && (
          <a
            href={`/sessions/${p.session_id}`}
            className="mt-1 inline-flex items-center gap-1 rounded-md bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700"
          >
            Open meeting <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>
    );
  }

  if (kind === 'action_item') {
    return (
      <div className="space-y-1.5">
        <div className="font-medium text-slate-900">{p.text || node.name}</div>
        {p.owner && <DetailRow label="Owner">{p.owner}</DetailRow>}
        {p.status && <DetailRow label="Status">{titleCase(p.status)}</DetailRow>}
        {fmtDate(p.due_date) && <DetailRow label="Due">{fmtDate(p.due_date)}</DetailRow>}
      </div>
    );
  }

  if (kind === 'decision' || kind === 'topic') {
    return (
      <div className="space-y-1.5">
        <div className="font-medium text-slate-900">{p.text || node.name}</div>
        {p.decided_by && <DetailRow label="Decided by">{p.decided_by}</DetailRow>}
        {p.parent_meeting && <DetailRow label="Meeting">{p.parent_meeting}</DetailRow>}
      </div>
    );
  }

  return <div className="text-sm font-medium text-slate-800">{node.name}</div>;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface BrigadeGraphViewerProps {
  /** Fetch the per-session graph by id. Ignored when `data` is provided. */
  sessionId?: string;
  /** Pre-fetched graph payload. When set, the viewer renders it directly and
   *  skips its own fetch (used by the cross-meeting Knowledge Graph page,
   *  which owns the fetch + its own empty-state copy). */
  data?: BrigadeGraphResponse;
  height?: number;
  showLabels?: boolean;
  /** Re-focus the graph on a clicked person (Knowledge Graph page passes this
   *  so you can walk the graph by clicking co-speakers). */
  onSelectSpeaker?: (speakerId: number, name: string) => void;
}

const BrigadeGraphViewer: React.FC<BrigadeGraphViewerProps> = ({
  sessionId,
  data: externalData,
  height = 480,
  showLabels = true,
  onSelectSpeaker,
}) => {
  const { getOrgQueryUrl } = useOrg();
  const containerRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<unknown>(null);

  const [data, setData] = useState<BrigadeGraphResponse | null>(externalData ?? null);
  const [loading, setLoading] = useState(!externalData);
  const [error, setError] = useState<string | null>(null);
  const [containerWidth, setContainerWidth] = useState<number>(0);
  const [selectedNode, setSelectedNode] = useState<BrigadeNode | null>(null);

  // Measure container width so the canvas fits its parent on resize.
  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const update = () => setContainerWidth(el.clientWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const fetchGraph = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = localStorage.getItem('access_token');
      const url = getOrgQueryUrl(
        `${config.apiUrl}/api/sessions/${sessionId}/brigade-graph`,
      );
      const resp = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
      }
      const body: BrigadeGraphResponse = await resp.json();
      setData(body);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [sessionId, getOrgQueryUrl]);

  useEffect(() => {
    // Parent-supplied data renders directly (no fetch) — the Knowledge Graph
    // page drives the viewer this way.
    if (externalData) {
      setData(externalData);
      setError(null);
      setLoading(false);
      return;
    }
    if (!sessionId) {
      setLoading(false);
      return;
    }
    fetchGraph();
  }, [externalData, sessionId, fetchGraph]);

  // react-force-graph-3d wants nodes + links to be REFERENCES that
  // match (link.source === node.id). We pass the response shape
  // directly; the library does its own resolution. But on re-render
  // we must avoid mutating the same array (the library stores
  // simulation state on the objects) so we memoize a per-fetch copy.
  const graphData = useMemo(() => {
    if (!data || !data.nodes.length) {
      return { nodes: [], links: [] };
    }
    return {
      nodes: data.nodes.map((n) => ({ ...n })),
      links: data.links.map((l) => ({ ...l })),
    };
  }, [data]);

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  if (loading) {
    return (
      <div
        ref={containerRef}
        className="bg-slate-50 border border-slate-200 rounded-lg p-6 flex items-center justify-center"
        style={{ height }}
      >
        <div className="flex items-center gap-3 text-slate-600 text-sm">
          <RefreshCw className="w-4 h-4 animate-spin" />
          <span>Loading the knowledge graph…</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div
        ref={containerRef}
        className="bg-rose-50 border border-rose-200 rounded-lg p-4"
      >
        <p className="text-sm text-rose-900 mb-3">
          Couldn't load the knowledge graph: {error}
        </p>
        <button
          onClick={fetchGraph}
          className="px-3 py-1.5 bg-rose-600 text-white text-xs font-medium rounded-md hover:bg-rose-700 transition-colors inline-flex items-center gap-2"
        >
          <RefreshCw className="w-3 h-3" />
          Retry
        </button>
      </div>
    );
  }

  if (!data || data.reason === 'not_synced_yet' || data.nodes.length === 0) {
    const isNotSynced = !data || data.reason === 'not_synced_yet';
    return (
      <div
        ref={containerRef}
        className="bg-slate-50 border border-slate-200 rounded-lg p-6 text-sm text-slate-700"
      >
        {isNotSynced ? (
          <p>
            This meeting isn't in the knowledge graph yet — it's added
            automatically once the meeting finishes processing. Record (or
            reprocess) a meeting and it'll appear here, and on the Knowledge
            Graph page in the sidebar.
          </p>
        ) : (
          <div>
            <p className="mb-3">
              The knowledge graph is temporarily unavailable for this meeting.
            </p>
            <button
              onClick={fetchGraph}
              className="px-3 py-1.5 bg-slate-700 text-white text-xs font-medium rounded-md hover:bg-slate-800 transition-colors inline-flex items-center gap-2"
            >
              <RefreshCw className="w-3 h-3" />
              Retry
            </button>
          </div>
        )}
      </div>
    );
  }

  // ------------------------------------------------------------------
  // 3D graph — rendered only when nodes are present.
  // ------------------------------------------------------------------
  return (
    <div ref={containerRef} className="space-y-3">
      <div
        className="bg-slate-900 rounded-lg overflow-hidden border border-slate-200 relative"
        style={{ height }}
      >
        {containerWidth > 0 && (
          <ForceGraph3D
            // react-force-graph-3d's ref typing is generic and depends
            // on the node/link generic params; we don't use the imperative
            // API today so a loose any-cast keeps the call-site type-clean
            // without dragging in ForceGraphMethods from the lib.
            ref={fgRef as React.MutableRefObject<any>}
            graphData={graphData}
            width={containerWidth}
            height={height}
            backgroundColor="#0f172a"
            nodeColor={(n: unknown) => colorForLabel((n as BrigadeNode).label)}
            nodeVal={(n: unknown) => sizeForNode(n as BrigadeNode)}
            nodeLabel={(n: unknown) => {
              const node = n as BrigadeNode;
              return showLabels ? `${node.label}: ${node.name}` : node.name;
            }}
            linkLabel={(l: unknown) => (l as BrigadeLink).type}
            linkColor={() => 'rgba(148, 163, 184, 0.6)'}
            linkWidth={1}
            linkDirectionalParticles={1}
            linkDirectionalParticleSpeed={0.005}
            onNodeClick={(n: unknown) => setSelectedNode(n as BrigadeNode)}
            onBackgroundClick={() => setSelectedNode(null)}
            enableNodeDrag={true}
            cooldownTicks={120}
          />
        )}
        {/* Legend */}
        <div className="absolute top-2 left-2 bg-slate-800/80 backdrop-blur rounded-md px-3 py-2 text-xs text-slate-100 space-y-1">
          <div className="font-semibold mb-1 text-slate-300">Legend</div>
          {Object.entries(LABEL_COLORS).map(([label, color]) => (
            <div key={label} className="flex items-center gap-2">
              <span
                className="inline-block w-2.5 h-2.5 rounded-full"
                style={{ backgroundColor: color }}
              />
              <span>{label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Selected node detail panel — appears below the canvas when a
          node is clicked. Click background to dismiss. */}
      {selectedNode && (
        <div className="bg-white border border-slate-200 rounded-lg p-4 text-sm shadow-sm">
          <div className="mb-2.5 flex items-center justify-between gap-3">
            <span
              className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium"
              style={{
                backgroundColor: colorForLabel(selectedNode.label) + '22',
                color: colorForLabel(selectedNode.label),
              }}
            >
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: colorForLabel(selectedNode.label) }}
              />
              {titleCase(selectedNode.label)}
            </span>
            <button
              onClick={() => setSelectedNode(null)}
              className="text-xs text-slate-400 hover:text-slate-600"
              aria-label="Close detail"
            >
              Close
            </button>
          </div>
          <NodeDetailBody node={selectedNode} onSelectSpeaker={onSelectSpeaker} />
        </div>
      )}

      {/* Footer: node/edge count. */}
      {data.nodes.length > 0 && (
        <div className="flex items-center justify-between gap-3 text-xs text-slate-600 px-1">
          <span>
            {data.nodes.length} node{data.nodes.length === 1 ? '' : 's'},{' '}
            {data.links.length} edge{data.links.length === 1 ? '' : 's'}
            {data.synced_at && (
              <>
                {' '}
                updated {new Date(data.synced_at).toLocaleString()}
              </>
            )}
          </span>
        </div>
      )}
    </div>
  );
};

export default BrigadeGraphViewer;
