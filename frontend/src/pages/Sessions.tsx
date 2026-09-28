import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import {
  Plus, Search, Filter, Calendar, Mic, Clock, Users,
  FileText, Download, Trash2, Play, Eye, Tag,
  ChevronDown, ChevronUp, Grid, List, SortAsc, SortDesc,
  Upload, FolderOpen, Star, Archive, MessageSquare, X, Loader2,
  ArrowUp, ArrowDown, ArrowUpDown, Briefcase, AlertTriangle,
  Lock as LockIcon, Paperclip, RefreshCw,
} from 'lucide-react';
import { SessionCreator } from '../components/SessionCreator';
import { SkeletonSessionGrid } from '../components/Skeleton';
import { cacheKey, useHydratedState } from '../utils/cachedState';
import { config } from '../config';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  TranscriptionOptionsPanel,
  DEFAULT_TRANSCRIPTION_OPTIONS,
  serializeTranscriptionOptions,
  type TranscriptionOptions,
} from '../components/TranscriptionOptionsPanel';
import { useUploads } from '../contexts/UploadsContext';
import { useOrg } from '../contexts/OrgContext';
import { useAuth } from '../contexts/AuthContext';
import { TagChip } from '../components/TagChip';
import MobileSessionsList from '../components/MobileSessionsList';
import { UpgradeBanner } from '../components/UpgradeBanner';
import { useTierFeatures } from '../hooks/useTierFeatures';
import {
  deleteLocalSession,
  isLocalSessionId,
  listLocalSessions,
  updateLocalSessionMeta,
  type LocalSession,
} from '../services/localSessionStore';
import { showToast } from '../components/Toast';
import { showConfirm } from '../utils/notifications';

interface Participant {
  id: string;
  name: string;
  email?: string | null;
  role?: string | null;
}

interface SessionSpeaker {
  name: string;
  photo_url?: string | null;   // MO-local SpeakerProfile photo (no live Contact-Ops call)
  raw_label?: string | null;   // e.g. SPEAKER_00
}

interface Session {
  id: string;
  name: string;
  title?: string;              // AI-generated title
  description?: string;
  created_at: string;
  updated_at: string;
  /** User-editable meeting date (ISO YYYY-MM-DD), distinct from
   *  created_at. Backfilled from started_at / created_at by alembic 027. */
  meeting_date?: string | null;
  /** User-editable 24h time of day (HH:MM:SS). */
  meeting_time?: string | null;
  duration?: number;
  status: 'idle' | 'active' | 'recording' | 'completed' | 'processing' | 'failed';
  participants?: Participant[];
  tags?: string[];
  audio_file?: string;
  transcript?: string;
  summary?: any;               // AI-generated summary (legacy nest: summary.analysis.*)
  final_summary?: any;         // New-style canonical summary (final_summary.executive/actions)
  summary_preview?: string;    // Plain-text snippet for list views
  speaker_count?: number;      // Distinct speakers, server-computed
  speakers?: SessionSpeaker[]; // Named speakers (display name + local photo) for cards
  project_app?: string | null;     // 'project-ops' | 'crisis-ops' | null
  project_id?: number | null;
  project_slug?: string | null;
  organization_id?: number | null;
  organization_name?: string | null;
  // Provenance: whose account captured/uploaded this session (workspaces
  // are shared; two members can record the same call).
  recorded_by?: string | null;
  has_transcript: boolean;
  has_audio: boolean;
  session_type?: 'live' | 'upload';
  /** True for local-only (privacy-mode) sessions read from IndexedDB. */
  is_local?: boolean;
  /** Processing metadata. `reprocess_status` tracks the server full-audio
   *  pipeline when an already-completed session is re-reprocessed: it flips
   *  to queued/in_progress while the top-level `status` stays 'completed',
   *  so the list row needs this to show any indicator. null/undefined for
   *  privacy-mode or never-uploaded sessions. */
  metadata?: {
    reprocess_status?: 'queued' | 'in_progress' | 'complete' | 'failed' | 'skipped' | null;
    [key: string]: unknown;
  };
}

interface OrgTagSummary {
  tag: string;
  count: number;
}

const UNGROUPED_FOLDER = '__ungrouped__';

function localSessionToSession(local: LocalSession): Session {
  // Map the IndexedDB record into the same shape the server list uses
  // so the existing table/card components don't need to branch.
  const endedAt = local.endedAt || local.startedAt;
  return {
    id: local.id,
    name: local.title,
    title: local.title,
    description: local.finalSummary || '',
    created_at: local.startedAt,
    updated_at: endedAt,
    duration: local.durationSeconds,
    status: local.endedAt ? 'completed' : 'active',
    participants: local.participants.map((name) => ({ id: name, name })),
    tags: local.tags,
    transcript: local.transcript.map((c) => c.text).join('\n'),
    summary_preview: (local.finalSummary || local.slices.map((s) => s.text).join('\n\n')).slice(0, 240),
    speaker_count: 0,
    has_transcript: local.transcript.length > 0,
    has_audio: false,
    session_type: 'live',
    is_local: true,
  };
}

/**
 * Read-only "Reprocessing…" pill for list rows / cards. When an already
 * completed session is re-reprocessed, the server full-audio pipeline flips
 * `metadata.reprocess_status` to queued/in_progress while the top-level
 * `status` stays 'completed' — so the status badge alone shows no change
 * (task 56f62de3). This surfaces that in-flight state. Display only; it does
 * not affect how status is computed. Renders nothing otherwise.
 */
function ReprocessingBadge({
  session,
  className = '',
}: {
  session: Session;
  className?: string;
}) {
  const rs = session.metadata?.reprocess_status;
  if (rs !== 'queued' && rs !== 'in_progress') return null;
  return (
    <span
      title="Server is reprocessing this session from full audio"
      className={`inline-flex items-center gap-1 rounded-full border border-blue-500/30 bg-blue-500/20 text-blue-300 text-xs ${className}`}
    >
      <span className="h-2 w-2 rounded-full border border-blue-300 border-t-transparent animate-spin" />
      Reprocessing…
    </span>
  );
}

interface RagMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: {session_id: string; title: string; score: number}[];
  rewrittenQuery?: string | null;
}

type SortKey = 'date' | 'name' | 'duration' | 'speakers' | 'status';
type GroupingMode = 'flat' | 'folders';
/**
 * dateBasis controls what the 'date' column + 'date' sort key actually
 * compare against:
 *   - 'upload'  → created_at (server ingest time, existing default)
 *   - 'meeting' → meeting_date (user-editable "when it actually happened")
 *
 * Lives in localStorage so a user who imports 526 historical Voice Memos
 * doesn't have to flip the toggle every session.
 */
type DateBasis = 'upload' | 'meeting';

const VIEW_MODE_KEY = 'meetingops.sessionsViewMode.v1';
const DATE_BASIS_KEY = 'meetingops.sessionsDateBasis.v1';
const VALID_SORT_KEYS: ReadonlyArray<SortKey> = ['date', 'name', 'duration', 'speakers', 'status'];

function normalizeTagSummaries(payload: unknown): OrgTagSummary[] {
  if (!payload) return [];
  const counts: Record<string, number> = {};
  let tags: string[] = [];

  if (Array.isArray(payload)) {
    tags = payload.filter((tag): tag is string => typeof tag === 'string' && tag.length > 0);
  } else if (typeof payload === 'object') {
    const data = payload as {
      tags?: Array<string | { tag?: string; name?: string; count?: number }>;
      tag_counts?: Record<string, number>;
    };
    if (Array.isArray(data.tags)) {
      for (const item of data.tags) {
        if (typeof item === 'string') {
          tags.push(item);
        } else if (item && typeof item === 'object') {
          const tag = item.tag || item.name;
          if (tag) {
            tags.push(tag);
            if (typeof item.count === 'number') {
              counts[tag] = item.count;
            }
          }
        }
      }
    }
    if (data.tag_counts && typeof data.tag_counts === 'object') {
      for (const [tag, count] of Object.entries(data.tag_counts)) {
        if (tag) counts[tag] = Number(count) || 0;
      }
    }
  }

  const deduped = Array.from(new Set(tags));
  return deduped
    .map((tag) => ({ tag, count: counts[tag] ?? 0 }))
    .sort((a, b) => a.tag.toLowerCase().localeCompare(b.tag.toLowerCase()));
}

function readDateBasis(): DateBasis {
  try {
    const v = localStorage.getItem(DATE_BASIS_KEY);
    return v === 'meeting' ? 'meeting' : 'upload';
  } catch {
    return 'upload';
  }
}

/** Pull the comparable date string for sort + display, honoring dateBasis.
 *  When dateBasis = 'meeting' and meeting_date is null, falls back to
 *  created_at so a session never disappears from the date column. */
function effectiveSessionDate(session: Session, basis: DateBasis): string {
  if (basis === 'meeting' && session.meeting_date) {
    return session.meeting_date;
  }
  return session.created_at;
}

/** Day-header label for date dividers: Today / Yesterday / weekday / full date. */
function dayHeaderLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'Undated';
  const a = new Date(); a.setHours(0, 0, 0, 0);
  const b = new Date(d); b.setHours(0, 0, 0, 0);
  const diff = Math.round((a.getTime() - b.getTime()) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  if (diff > 1 && diff < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/** The group a session falls under for the active sort, or null when the sort
 *  mode isn't grouped (name / duration stay a flat list). */
function sessionGroupLabel(session: Session, sortBy: SortKey, basis: DateBasis): string | null {
  if (sortBy === 'date') return dayHeaderLabel(effectiveSessionDate(session, basis));
  if (sortBy === 'speakers') {
    const bucket = SPEAKER_BUCKETS.find((bk) => bk.test(deriveSpeakerCount(session)));
    return bucket ? bucket.label : 'Speakers';
  }
  if (sortBy === 'status') {
    const s = session.status || 'unknown';
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  return null;
}

/** Divider label to render BEFORE item i (when its group differs from the
 *  previous item's), else null — drives the date/sort group headers. */
function sessionDividerLabel(
  list: Session[], i: number, sortBy: SortKey, basis: DateBasis,
): string | null {
  const label = sessionGroupLabel(list[i], sortBy, basis);
  if (!label) return null;
  const prev = i > 0 ? sessionGroupLabel(list[i - 1], sortBy, basis) : null;
  return label !== prev ? label : null;
}

function deriveSpeakerCount(session: Session): number {
  return typeof session.speaker_count === 'number' ? session.speaker_count : 0;
}

function truncate(text: string, max: number): string {
  if (!text) return '';
  if (text.length <= max) return text;
  return text.slice(0, max - 1) + '…';
}

interface SessionsTableProps {
  sessions: Session[];
  sortBy: SortKey;
  sortOrder: 'asc' | 'desc';
  onHeaderSort: (key: SortKey) => void;
  statusFilter: string[];
  speakerFilter: string[];
  onStatusFilterChange: (values: string[]) => void;
  onSpeakerFilterChange: (values: string[]) => void;
  openColumnFilter: null | 'status' | 'speakers';
  onOpenColumnFilter: (col: null | 'status' | 'speakers') => void;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
  selectedSessionIds: string[];
  onToggleSessionSelection: (id: string) => void;
  /** Toggle selection for every currently-visible (post column-filter) row.
   *  Passed the ids the table is actually rendering so "select all" never
   *  touches rows hidden by the status/speaker column filters. */
  onToggleSelectVisible: (visibleIds: string[], selectAll: boolean) => void;
  formatDuration: (seconds?: number) => string;
  getStatusColor: (status: string) => string;
  getStatusLabel: (status: string) => string;
  dateBasis: DateBasis;
}

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'completed', label: 'Completed' },
  { value: 'processing', label: 'Processing' },
  { value: 'active', label: 'Recording' },
  { value: 'idle', label: 'Idle' },
  { value: 'failed', label: 'Failed' },
];

const SPEAKER_BUCKETS: Array<{ value: string; label: string; test: (n: number) => boolean }> = [
  { value: '0', label: 'No speakers', test: (n) => n === 0 },
  { value: '1', label: '1 speaker', test: (n) => n === 1 },
  { value: '2-3', label: '2–3 speakers', test: (n) => n >= 2 && n <= 3 },
  { value: '4+', label: '4 or more', test: (n) => n >= 4 },
];

const SessionsTable: React.FC<SessionsTableProps> = ({
  sessions,
  sortBy,
  sortOrder,
  onHeaderSort,
  statusFilter,
  speakerFilter,
  onStatusFilterChange,
  onSpeakerFilterChange,
  openColumnFilter,
  onOpenColumnFilter,
  onOpen,
  onDelete,
  selectedSessionIds,
  onToggleSessionSelection,
  onToggleSelectVisible,
  formatDuration,
  getStatusColor,
  getStatusLabel,
  dateBasis,
}) => {
  const filtered = useMemo(() => {
    return sessions.filter((s) => {
      if (statusFilter.length > 0 && !statusFilter.includes(s.status)) return false;
      if (speakerFilter.length > 0) {
        const n = deriveSpeakerCount(s);
        const ok = speakerFilter.some((v) => {
          const b = SPEAKER_BUCKETS.find((x) => x.value === v);
          return b ? b.test(n) : false;
        });
        if (!ok) return false;
      }
      return true;
    });
  }, [sessions, statusFilter, speakerFilter]);

  // Ids the table is actually rendering (after column filters). "Select
  // all" operates on exactly this set so it never reaches hidden rows.
  const visibleIds = useMemo(() => filtered.map((s) => s.id), [filtered]);
  const selectedSet = useMemo(() => new Set(selectedSessionIds), [selectedSessionIds]);
  const visibleSelectedCount = useMemo(
    () => visibleIds.reduce((acc, id) => (selectedSet.has(id) ? acc + 1 : acc), 0),
    [visibleIds, selectedSet],
  );
  const allVisibleSelected = visibleIds.length > 0 && visibleSelectedCount === visibleIds.length;
  const someVisibleSelected = visibleSelectedCount > 0 && !allVisibleSelected;
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (selectAllRef.current) {
      // indeterminate has no JSX attribute; set it imperatively.
      selectAllRef.current.indeterminate = someVisibleSelected;
    }
  }, [someVisibleSelected]);

  const renderSortIcon = (key: SortKey) => {
    if (sortBy !== key) {
      return <ArrowUpDown className="w-3.5 h-3.5 text-gray-500 opacity-0 group-hover:opacity-100 transition-opacity" />;
    }
    return sortOrder === 'asc'
      ? <ArrowUp className="w-3.5 h-3.5 text-purple-300" />
      : <ArrowDown className="w-3.5 h-3.5 text-purple-300" />;
  };

  const headerBtn = (key: SortKey, label: string, extra?: string) => (
    <button
      type="button"
      onClick={() => onHeaderSort(key)}
      className={`group inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-300 hover:text-white transition-colors ${extra || ''}`}
      aria-sort={sortBy === key ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <span>{label}</span>
      {renderSortIcon(key)}
    </button>
  );

  const toggleMulti = (current: string[], v: string, setter: (vals: string[]) => void) => {
    if (current.includes(v)) setter(current.filter((x) => x !== v));
    else setter([...current, v]);
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="max-h-[calc(100vh-260px)] overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 z-10 bg-gray-900/95 backdrop-blur">
            <tr className="border-b border-gray-800">
              <th className="px-3 py-3 text-left w-10">
                <input
                  ref={selectAllRef}
                  type="checkbox"
                  checked={allVisibleSelected}
                  disabled={visibleIds.length === 0}
                  onChange={(e) => onToggleSelectVisible(visibleIds, e.target.checked)}
                  className="h-4 w-4 rounded border-gray-600 bg-gray-900 text-purple-500 focus:ring-purple-500 disabled:opacity-40"
                  aria-label={allVisibleSelected ? 'Deselect all visible sessions' : 'Select all visible sessions'}
                  title={allVisibleSelected ? 'Deselect all visible' : 'Select all visible'}
                />
              </th>
              <th className="px-4 py-3 text-left">{headerBtn('name', 'Title')}</th>
              <th className="px-4 py-3 text-left whitespace-nowrap">{headerBtn('date', dateBasis === 'meeting' ? 'Meeting' : 'Uploaded')}</th>
              <th className="px-4 py-3 text-left whitespace-nowrap">{headerBtn('duration', 'Duration')}</th>
              <th className="px-4 py-3 text-left whitespace-nowrap">
                <div className="flex items-center gap-2">
                  {headerBtn('speakers', 'Speakers')}
                  <div className="relative" data-column-filter>
                    <button
                      type="button"
                      onClick={() => onOpenColumnFilter(openColumnFilter === 'speakers' ? null : 'speakers')}
                      className={`p-1 rounded hover:bg-gray-800 transition-colors ${speakerFilter.length > 0 ? 'text-purple-300' : 'text-gray-500 hover:text-gray-300'}`}
                      aria-label="Filter by speakers"
                      aria-expanded={openColumnFilter === 'speakers'}
                    >
                      <Filter className="w-3.5 h-3.5" />
                      {speakerFilter.length > 0 && (
                        <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-purple-500" />
                      )}
                    </button>
                    {openColumnFilter === 'speakers' && (
                      <div className="absolute left-0 top-7 w-44 rounded-lg border border-gray-700 bg-gray-900 shadow-xl p-2 z-20" data-column-filter>
                        {SPEAKER_BUCKETS.map((b) => {
                          const checked = speakerFilter.includes(b.value);
                          return (
                            <label key={b.value} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-800 cursor-pointer text-xs text-gray-200">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleMulti(speakerFilter, b.value, onSpeakerFilterChange)}
                                className="accent-purple-500"
                              />
                              {b.label}
                            </label>
                          );
                        })}
                        {speakerFilter.length > 0 && (
                          <button
                            type="button"
                            onClick={() => onSpeakerFilterChange([])}
                            className="mt-1 w-full text-xs text-gray-400 hover:text-white px-2 py-1 border-t border-gray-800"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </th>
              <th className="px-4 py-3 text-left whitespace-nowrap text-xs font-semibold uppercase tracking-wide text-gray-300">Participants</th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-300">Tags</th>
              <th className="px-4 py-3 text-left whitespace-nowrap">
                <div className="flex items-center gap-2">
                  {headerBtn('status', 'Status')}
                  <div className="relative" data-column-filter>
                    <button
                      type="button"
                      onClick={() => onOpenColumnFilter(openColumnFilter === 'status' ? null : 'status')}
                      className={`p-1 rounded hover:bg-gray-800 transition-colors ${statusFilter.length > 0 ? 'text-purple-300' : 'text-gray-500 hover:text-gray-300'}`}
                      aria-label="Filter by status"
                      aria-expanded={openColumnFilter === 'status'}
                    >
                      <Filter className="w-3.5 h-3.5" />
                      {statusFilter.length > 0 && (
                        <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-purple-500" />
                      )}
                    </button>
                    {openColumnFilter === 'status' && (
                      <div className="absolute right-0 top-7 w-44 rounded-lg border border-gray-700 bg-gray-900 shadow-xl p-2 z-20" data-column-filter>
                        {STATUS_OPTIONS.map((opt) => {
                          const checked = statusFilter.includes(opt.value);
                          return (
                            <label key={opt.value} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-800 cursor-pointer text-xs text-gray-200">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleMulti(statusFilter, opt.value, onStatusFilterChange)}
                                className="accent-purple-500"
                              />
                              {opt.label}
                            </label>
                          );
                        })}
                        {statusFilter.length > 0 && (
                          <button
                            type="button"
                            onClick={() => onStatusFilterChange([])}
                            className="mt-1 w-full text-xs text-gray-400 hover:text-white px-2 py-1 border-t border-gray-800"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </th>
              <th className="px-4 py-3 text-right whitespace-nowrap text-xs font-semibold uppercase tracking-wide text-gray-300">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={9} className="px-4 py-10 text-center text-gray-500 text-sm">
                  No sessions match the current filters.
                </td>
              </tr>
            ) : (
              filtered.map((session) => {
                const fullTitle = session.title || session.name || 'Untitled';
                const truncated = truncate(fullTitle, 60);
                const speakerCount = deriveSpeakerCount(session);
                const participants = session.participants || [];
                const tags = session.tags || [];
                const visibleTags = [...tags].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())).slice(0, 3);
                const hiddenTagCount = tags.length - visibleTags.length;
                return (
                  <tr
                    key={session.id}
                    className={`border-b border-gray-800/60 hover:bg-gray-800/40 transition-colors ${
                      selectedSessionIds.includes(session.id) ? 'bg-purple-500/10' : ''
                    }`}
                  >
                    <td className="px-3 py-3 align-middle">
                      <input
                        type="checkbox"
                        checked={selectedSessionIds.includes(session.id)}
                        onChange={() => onToggleSessionSelection(session.id)}
                        className="h-4 w-4 rounded border-gray-600 bg-gray-900 text-purple-500 focus:ring-purple-500"
                        aria-label="Select session"
                      />
                    </td>
                    <td className="px-4 py-3 align-middle">
                      <div className="flex items-center gap-2">
                        {session.is_local && (
                          <span
                            title="Local-only session (privacy mode). Stored only in this browser."
                            className="inline-flex items-center rounded-full border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-200"
                          >
                            <LockIcon className="w-3 h-3 mr-1" />
                            Local
                          </span>
                        )}
                        {!session.is_local && session.organization_name && (
                          <span
                            title={`Organization: ${session.organization_name}`}
                            className="inline-flex items-center rounded-full border border-gray-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium text-gray-300"
                          >
                            {session.organization_name}
                          </span>
                        )}
                        {session.recorded_by && (
                          <span
                            title={`Recorded on ${session.recorded_by}'s account`}
                            className="inline-flex items-center rounded-full border border-gray-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium text-gray-400"
                          >
                            rec: {session.recorded_by}
                          </span>
                        )}
                        <button
                          type="button"
                          onClick={() => onOpen(session.id)}
                          title={fullTitle}
                          className="block text-left font-medium text-white hover:text-purple-300 transition-colors max-w-[44ch] truncate"
                        >
                          {truncated}
                        </button>
                      </div>
                      {session.project_app && session.project_slug && (
                        <span
                          className={`mt-1 inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] ${
                            session.project_app === 'crisis-ops'
                              ? 'border-red-800/60 bg-red-900/20 text-red-300'
                              : 'border-purple-800/60 bg-purple-900/20 text-purple-300'
                          }`}
                          title={`Linked to ${session.project_app} / ${session.project_slug}`}
                        >
                          {session.project_app === 'crisis-ops'
                            ? <AlertTriangle className="w-2.5 h-2.5" />
                            : <Briefcase className="w-2.5 h-2.5" />}
                          {session.project_slug}
                        </span>
                      )}
                      {session.summary_preview && (
                        <div
                          className="mt-1 text-xs text-gray-400 max-w-[44ch] line-clamp-2"
                          title={session.summary_preview}
                        >
                          {session.summary_preview}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 align-middle whitespace-nowrap text-gray-300">
                      {(() => {
                        const d = effectiveSessionDate(session, dateBasis);
                        if (!d) return '—';
                        // meeting_date is plain YYYY-MM-DD without TZ;
                        // pin it to local midnight to avoid an off-by-one
                        // when the browser TZ is west of UTC.
                        const parsed = d.length === 10
                          ? new Date(d + 'T00:00:00')
                          : new Date(d);
                        return parsed.toLocaleDateString();
                      })()}
                    </td>
                    <td className="px-4 py-3 align-middle whitespace-nowrap text-gray-300 tabular-nums">
                      {formatDuration(session.duration)}
                    </td>
                    <td className="px-4 py-3 align-middle whitespace-nowrap text-gray-300 tabular-nums">
                      {speakerCount > 0 ? speakerCount : <span className="text-gray-600">—</span>}
                    </td>
                    <td className="px-4 py-3 align-middle text-gray-300">
                      {participants.length > 0 ? (
                        <span
                          className="inline-block max-w-[28ch] truncate align-middle"
                          title={participants.map((p) => p.name).join(', ')}
                        >
                          {participants.slice(0, 2).map((p) => p.name).join(', ')}
                          {participants.length > 2 && ` +${participants.length - 2}`}
                        </span>
                      ) : (
                        <span className="text-gray-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 align-middle">
                      {visibleTags.length > 0 ? (
                        <div className="flex flex-wrap gap-1 max-w-[32ch]">
                          {visibleTags.map((tag) => (
                            <TagChip key={tag} tag={tag} />
                          ))}
                          {hiddenTagCount > 0 && (
                            <span
                              className="px-2 py-0.5 text-xs rounded-full border border-gray-700 bg-gray-800 text-gray-400"
                              title={tags.slice(visibleTags.length).join(', ')}
                            >
                              +{hiddenTagCount}
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="text-gray-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 align-middle whitespace-nowrap">
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs border ${getStatusColor(session.status)}`}>
                        {getStatusLabel(session.status)}
                      </span>
                      <ReprocessingBadge session={session} className="ml-1 px-2 py-0.5" />
                    </td>
                    <td className="px-4 py-3 align-middle text-right whitespace-nowrap">
                      <div className="inline-flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => onOpen(session.id)}
                          className="p-2 rounded-md text-gray-300 hover:text-white hover:bg-purple-600/30 transition-colors"
                          title="Open session"
                          aria-label="Open session"
                        >
                          <Eye className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => onDelete(session.id)}
                          className="p-2 rounded-md text-red-400 hover:text-red-300 hover:bg-red-600/20 transition-colors"
                          title="Delete session"
                          aria-label="Delete session"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <div className="px-4 py-2 border-t border-gray-800 text-xs text-gray-500 flex items-center justify-between">
        <span>
          {filtered.length} {filtered.length === 1 ? 'session' : 'sessions'}
          {(statusFilter.length > 0 || speakerFilter.length > 0) && (
            <span className="text-gray-600"> (filtered from {sessions.length})</span>
          )}
        </span>
        {(statusFilter.length > 0 || speakerFilter.length > 0) && (
          <button
            type="button"
            onClick={() => {
              onStatusFilterChange([]);
              onSpeakerFilterChange([]);
            }}
            className="text-gray-400 hover:text-white"
          >
            Clear column filters
          </button>
        )}
      </div>
    </div>
  );
};

/**
 * One bulk action in the selection toolbar. Built as a list so additional
 * bulk operations (move to folder, export, tag, …) can be added later by
 * pushing more entries — Delete is the only one shipped today.
 */
interface BulkAction {
  key: string;
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  /** Visual emphasis. 'danger' is the destructive red treatment. */
  variant?: 'default' | 'danger';
  disabled?: boolean;
  busy?: boolean;
}

interface BulkActionsToolbarProps {
  selectedCount: number;
  actions: BulkAction[];
  onClear: () => void;
}

/**
 * Sticky bulk-action bar shown whenever >= 1 session is selected. Pinned to
 * the bottom of the (desktop) sessions surface so it stays reachable while
 * the user scrolls a long list. Styling mirrors the existing Sessions
 * Tailwind tokens (gray-900 surface, purple accent, red danger).
 */
const BulkActionsToolbar: React.FC<BulkActionsToolbarProps> = ({
  selectedCount,
  actions,
  onClear,
}) => {
  if (selectedCount <= 0) return null;
  return (
    <div className="sticky bottom-0 z-30 mt-4 -mx-6 border-t border-gray-800 bg-gray-900/95 px-6 py-3 backdrop-blur supports-[backdrop-filter]:bg-gray-900/80">
      <div className="flex flex-wrap items-center gap-3">
        <span className="inline-flex items-center gap-2 text-sm font-medium text-white">
          <span className="inline-flex h-6 min-w-6 items-center justify-center rounded-full bg-purple-600 px-2 text-xs font-semibold text-white">
            {selectedCount}
          </span>
          selected
        </span>
        <div className="h-5 w-px bg-gray-700" aria-hidden="true" />
        {actions.map((action) => (
          <button
            key={action.key}
            type="button"
            onClick={action.onClick}
            disabled={action.disabled || action.busy}
            className={
              action.variant === 'danger'
                ? 'inline-flex items-center gap-2 rounded-lg border border-red-600/50 bg-red-600/20 px-3 py-2 text-sm font-medium text-red-300 transition-colors hover:bg-red-600/30 disabled:cursor-not-allowed disabled:opacity-50'
                : 'inline-flex items-center gap-2 rounded-lg bg-purple-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-purple-500 disabled:cursor-not-allowed disabled:opacity-50'
            }
          >
            {action.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : action.icon}
            {action.label}
          </button>
        ))}
        <button
          type="button"
          onClick={onClear}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium text-gray-400 transition-colors hover:bg-gray-800 hover:text-white"
        >
          <X className="h-4 w-4" />
          Clear
        </button>
      </div>
    </div>
  );
};

export const Sessions: React.FC = () => {
  const navigate = useNavigate();
  const { startUploads } = useUploads();
  const { activeOrganization, organizations = [] } = useOrg();
  // v3.23.0 — gate the "Upload Audio/Video" surface on the new
  // `audio_upload` tier flag. Free + Basic stay browser-only for audio;
  // Pro+ keeps the existing upload path. The hook defaults to false
  // while /api/auth/me is in flight so the button doesn't flash for
  // free users on first paint.
  const { hasFeature } = useTierFeatures();
  const canUploadAudio = hasFeature('audio_upload');
  const [searchParams, setSearchParams] = useSearchParams();
  // Active tag filter chips (AND). Server applies the filter via ?tags=
  // so the UI never has to reason about which sessions to hide locally.
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  // Hydrate sessions from localStorage cache so the list paints
  // immediately on refresh. Cache key includes orgSlug + tag selection
  // so wrong-org / wrong-filter data is never read.
  const sessionsCacheKey = cacheKey(
    'sessions',
    activeOrganization?.slug,
    selectedTags.length > 0 ? selectedTags.slice().sort().join(',') : 'all',
  );
  const [sessions, setSessions, sessionsHydratedFromCache] = useHydratedState<Session[]>(
    sessionsCacheKey,
    [],
  );
  const [filteredSessions, setFilteredSessions] = useState<Session[]>([]);
  // How many session cards to render at once. The list fetches up to ~100 from
  // the server; rendering all of them (each with avatars) is heavy, so cap the
  // DOM by default with a user-controlled page size ('all' renders everything).
  const [pageSize, setPageSize] = useState<number | 'all'>(25);
  // The capped slice actually rendered (grid / list / mobile all use this).
  const displayedSessions = useMemo(
    () => (pageSize === 'all' ? filteredSessions : filteredSessions.slice(0, pageSize)),
    [filteredSessions, pageSize],
  );
  // All tags used across the org's sessions, plus counts for the folder rail.
  const [orgTagSummaries, setOrgTagSummaries] = useState<OrgTagSummary[]>([]);
  const [orgTagStats, setOrgTagStats] = useState({ totalSessions: 0, untaggedSessions: 0 });
  // session_pk (as string) -> attachment count. Used by the list cards to
  // show a paperclip icon when a session has any attachments. Fetched
  // once per active-org swap from the dedicated counts endpoint so we
  // don't pay N round-trips against /attachments per card.
  const [attachmentCounts, setAttachmentCounts] = useState<Record<string, number>>({});
  // Loading is only true on the very first mount when nothing is cached.
  // Subsequent polls + filter changes don't toggle this — they update
  // the list in place.
  const [loading, setLoading] = useState(!sessionsHydratedFromCache);
  // True once the first server fetch has completed. Gates the "No sessions"
  // empty-state so it can't flash during the initial load — when the list is
  // hydrated from an empty cache, `loading` is already false but the real data
  // hasn't arrived yet, which would otherwise show "No sessions found" for ~1s.
  const [hasFetchedOnce, setHasFetchedOnce] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  // Set when the server sessions fetch fails — drives a "couldn't refresh"
  // banner (cached list preserved) instead of a misleading empty state.
  const [serverError, setServerError] = useState<string | null>(null);
  // Auto-refresh: poll the list while it's on (default on) so a new/just-
  // processed meeting appears without a manual reload; the Refresh button
  // forces it immediately.
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [showCreator, setShowCreator] = useState(false);
  // viewMode persists to localStorage; mobile shell ignores it (always card list).
  const [viewMode, setViewMode] = useState<'grid' | 'list'>(() => {
    try {
      const stored = localStorage.getItem(VIEW_MODE_KEY);
      return stored === 'list' ? 'list' : 'grid';
    } catch {
      return 'grid';
    }
  });
  const [groupingMode, setGroupingMode] = useState<GroupingMode>('flat');
  // dateBasis decides whether the date column + 'date' sort use the
  // server upload time (default) or the user-editable meeting_date.
  // Persisted in localStorage so a user backfilling historical audio
  // doesn't have to flip it every visit.
  const [dateBasis, setDateBasis] = useState<DateBasis>(() => readDateBasis());
  useEffect(() => {
    try { localStorage.setItem(DATE_BASIS_KEY, dateBasis); } catch { /* non-fatal */ }
  }, [dateBasis]);
  const orgTags = useMemo(() => orgTagSummaries.map((item) => item.tag), [orgTagSummaries]);
  const tagCountMap = useMemo(
    () => Object.fromEntries(orgTagSummaries.map((item) => [item.tag, item.count])),
    [orgTagSummaries],
  );
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [typeFilter, setTypeFilter] = useState<string>('all');
  // Local-only privacy mode sessions are merged into the list. This
  // chip toggles between showing all, only local, or only synced
  // (server-stored). Default: all.
  const [sourceFilter, setSourceFilter] = useState<'all' | 'local' | 'synced'>('all');
  const [orgFilter, setOrgFilter] = useState<number | 'all'>('all');
  // Platform-admin god-view: when ON (superusers only), the list fetch crosses
  // every tenant instead of just the caller's orgs. Explicit + opt-in.
  const { user } = useAuth();
  const isSuperuser = !!user?.is_superuser;
  const [allTenants, setAllTenants] = useState(false);
  // Sort state hydrates from URL ?sort=&order= so refresh keeps the
  // user's column choice. Falls back to date-desc if missing/invalid.
  const initialSort: SortKey = (() => {
    const s = searchParams.get('sort');
    return s && (VALID_SORT_KEYS as ReadonlyArray<string>).includes(s) ? (s as SortKey) : 'date';
  })();
  const initialOrder: 'asc' | 'desc' = searchParams.get('order') === 'asc' ? 'asc' : 'desc';
  const [sortBy, setSortByState] = useState<SortKey>(initialSort);
  const [sortOrder, setSortOrderState] = useState<'asc' | 'desc'>(initialOrder);
  // Table-only column filters (multi-select).
  const [tableStatusFilter, setTableStatusFilter] = useState<string[]>([]);
  const [tableSpeakerFilter, setTableSpeakerFilter] = useState<string[]>([]);
  const [openColumnFilter, setOpenColumnFilter] = useState<null | 'status' | 'speakers'>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>([]);
  // True while a bulk operation (delete/export) is in flight; disables
  // the toolbar buttons so the user can't double-fire it.
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [bulkExporting, setBulkExporting] = useState(false);
  const [selectedFolder, setSelectedFolder] = useState<string>('all');
  const [folderMutationBusy, setFolderMutationBusy] = useState(false);
  const [searchResults, setSearchResults] = useState<Session[] | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(VIEW_MODE_KEY, viewMode);
    } catch { /* non-fatal */ }
  }, [viewMode]);

  // Keep URL params in sync when sort changes. Replace=true so back-button
  // doesn't fill up with every header click.
  const setSort = useCallback((key: SortKey, order: 'asc' | 'desc') => {
    setSortByState(key);
    setSortOrderState(order);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('sort', key);
      next.set('order', order);
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const handleHeaderSort = useCallback((key: SortKey) => {
    if (sortBy === key) {
      setSort(key, sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      // First click on a new column → sensible default: text asc, numbers desc.
      const defaultOrder: 'asc' | 'desc' = (key === 'name' || key === 'status') ? 'asc' : 'desc';
      setSort(key, defaultOrder);
    }
  }, [sortBy, sortOrder, setSort]);

  // Close column-filter popovers on outside click.
  useEffect(() => {
    if (!openColumnFilter) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (t && t.closest('[data-column-filter]')) return;
      setOpenColumnFilter(null);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [openColumnFilter]);

  // Cross-meeting RAG chat state (multi-turn)
  const [ragMessages, setRagMessages] = useState<RagMessage[]>([]);
  const [ragLoading, setRagLoading] = useState(false);
  const [showRagPanel, setShowRagPanel] = useState(false);
  const [ragFollowUp, setRagFollowUp] = useState('');
  const ragEndRef = useRef<HTMLDivElement>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);

  // Debounced server-side search (keyword + semantic)
  const performSearch = useCallback(async (query: string) => {
    if (!query || query.length < 2) {
      setSearchResults(null);
      setIsSearching(false);
      return;
    }
    setIsSearching(true);
    try {
      const headers: Record<string, string> = {};
      const token = localStorage.getItem('access_token');
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }

      // Run keyword search and semantic search in parallel
      const [keywordRes, semanticRes] = await Promise.allSettled([
        fetch(
          `${config.apiBaseUrl}/api/simple/recording-sessions/search?q=${encodeURIComponent(query)}`,
          { headers }
        ),
        fetch(
          `${config.apiBaseUrl}/api/simple/recording-sessions/semantic-search?q=${encodeURIComponent(query)}&limit=10`,
          { headers }
        ),
      ]);

      const seenIds = new Set<string>();
      const merged: Session[] = [];

      // Process keyword results first (exact matches)
      if (keywordRes.status === 'fulfilled' && keywordRes.value.ok) {
        const data = await keywordRes.value.json();
        for (const r of data) {
          if (!seenIds.has(r.id)) {
            seenIds.add(r.id);
            merged.push({
              id: r.id,
              name: r.name,
              description: r.description || '',
              created_at: r.created_at,
              updated_at: r.created_at,
              status: r.status || 'completed',
              duration: r.duration || 0,
              has_transcript: r.match_field === 'transcript',
              has_audio: !!(r.duration && r.duration > 0),
              _snippet: r.snippet,
              _matchField: r.match_field,
            } as Session);
          }
        }
      }

      // Append semantic results that weren't in keyword results
      if (semanticRes.status === 'fulfilled' && semanticRes.value.ok) {
        const data = await semanticRes.value.json();
        for (const r of (data.results || [])) {
          if (!seenIds.has(r.session_id) && r.score > 0.3) {
            seenIds.add(r.session_id);
            merged.push({
              id: r.session_id,
              name: r.title || 'Untitled',
              description: r.snippet || '',
              created_at: r.created_at || '',
              updated_at: r.created_at || '',
              status: 'completed',
              duration: 0,
              has_transcript: true,
              has_audio: false,
              _snippet: r.snippet,
              _matchField: `semantic (${Math.round(r.score * 100)}% match)`,
            } as Session);
          }
        }
      }

      setSearchResults(merged);
    } catch (err) {
      console.error('Search failed:', err);
    } finally {
      setIsSearching(false);
    }
  }, []);

  // Handle search input with debounce
  const handleSearchChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setSearchQuery(value);
    if (searchTimerRef.current) {
      clearTimeout(searchTimerRef.current);
    }
    searchTimerRef.current = setTimeout(() => {
      performSearch(value);
    }, 300);
  }, [performSearch]);

  // Cross-meeting RAG query (multi-turn)
  const sendRagMessage = useCallback(async (message: string) => {
    if (!message || message.length < 2) return;
    setRagLoading(true);
    setShowRagPanel(true);
    // Add user message to conversation
    setRagMessages(prev => [...prev, { role: 'user', content: message }]);
    try {
      const token = localStorage.getItem('access_token');
      const res = await fetch(`${config.apiBaseUrl}/api/ai-chat/rag/query`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ message, limit: 5 }),
      });
      if (res.ok) {
        const data = await res.json();
        setRagMessages(prev => [...prev, {
          role: 'assistant',
          content: data.answer || 'No answer generated.',
          sources: data.sources || [],
          rewrittenQuery: data.metadata?.rewritten_query || null,
        }]);
      } else {
        const errText = await res.text();
        setRagMessages(prev => [...prev, {
          role: 'assistant',
          content: `Error: ${res.status} - ${errText}`,
        }]);
      }
    } catch (err) {
      console.error('RAG query failed:', err);
      setRagMessages(prev => [...prev, {
        role: 'assistant',
        content: 'Failed to reach the AI service. Is the backend running?',
      }]);
    } finally {
      setRagLoading(false);
      setRagFollowUp('');
      setTimeout(() => ragEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 100);
    }
  }, []);

  // Initial RAG query from search bar
  const askAiAboutMeetings = useCallback(async () => {
    if (!searchQuery || searchQuery.length < 2) return;
    // Clear previous conversation when starting a new topic from search
    setRagMessages([]);
    // Also clear server-side history for a fresh start
    try {
      const token = localStorage.getItem('access_token');
      await fetch(`${config.apiBaseUrl}/api/ai-chat/rag/history`, {
        method: 'DELETE',
        headers: { ...(token ? { 'Authorization': `Bearer ${token}` } : {}) },
      });
    } catch { /* ignore */ }
    sendRagMessage(searchQuery);
  }, [searchQuery, sendRagMessage]);

  // Handle follow-up submit
  const handleRagFollowUp = useCallback(() => {
    if (!ragFollowUp.trim() || ragLoading) return;
    sendRagMessage(ragFollowUp.trim());
  }, [ragFollowUp, ragLoading, sendRagMessage]);

  // Load persisted RAG chat history from server on mount
  useEffect(() => {
    const loadRagHistory = async () => {
      try {
        const token = localStorage.getItem('access_token');
        const res = await fetch(`${config.apiBaseUrl}/api/ai-chat/rag/history`, {
          headers: { ...(token ? { 'Authorization': `Bearer ${token}` } : {}) },
        });
        if (res.ok) {
          const data = await res.json();
          // Validate that we got actual chat messages (not other data)
          if (Array.isArray(data) && data.length > 0 && data[0].role && data[0].content) {
            const restored: RagMessage[] = data.map((msg: any) => ({
              role: msg.role as 'user' | 'assistant',
              content: msg.content,
            }));
            setRagMessages(restored);
            setShowRagPanel(true);
          }
        }
      } catch {
        // Ignore - history loading is best-effort
      }
    };
    loadRagHistory();
  }, []);

  const refreshOrgTags = useCallback(async () => {
    try {
      const headers: any = {};
      const token = localStorage.getItem('access_token');
      if (token) headers['Authorization'] = `Bearer ${token}`;
      if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
      const res = await fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/tags`, { headers });
      if (!res.ok) return;
      const data = await res.json();
      const summaries = normalizeTagSummaries(data);
      setOrgTagSummaries(summaries);
      setOrgTagStats({
        totalSessions: Number(data?.total_sessions) || 0,
        untaggedSessions: Number(data?.untagged_sessions) || 0,
      });
    } catch { /* non-fatal */ }
  }, [activeOrganization?.slug]);

  useEffect(() => {
    void refreshOrgTags();
  }, [refreshOrgTags]);

  useEffect(() => {
    // If we already painted from cache, skip the spinner-on path —
    // the user sees yesterday's list (almost always identical) and
    // the refetch swaps in the latest in place. Only show the spinner
    // when there's genuinely nothing on screen yet.
    fetchSessions({ showSpinner: !sessionsHydratedFromCache });
    if (!autoRefresh) return;
    const interval = setInterval(() => fetchSessions({ showSpinner: false }), 10000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTags.join(','), activeOrganization?.slug, autoRefresh, allTenants]);

  useEffect(() => {
    const onInvalidated = () => fetchSessions({ showSpinner: false });
    window.addEventListener('meetingops:sessions-invalidated', onInvalidated);
    return () => window.removeEventListener('meetingops:sessions-invalidated', onInvalidated);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTags.join(','), activeOrganization?.slug]);

  useEffect(() => {
    filterSessions();
  }, [sessions, searchQuery, searchResults, statusFilter, typeFilter, sourceFilter, orgFilter, selectedFolder, sortBy, sortOrder, dateBasis]);

  // showSpinner=false on polling refresh so the list doesn't flash
  // through the empty-loading state every 10 seconds. Initial mount + filter
  // changes still show the spinner. Background ticks set `refreshing` so the
  // mobile shell can show a subtle "refreshing" indicator without ripping
  // the list out from under the user.
  //
  // Local-only sessions (privacy mode) live in IndexedDB and are merged
  // in here so the rest of the UI is unaware of the split. The merge
  // sorts by created_at descending so the chronological mix is correct.
  const fetchSessions = async ({
    showSpinner = true,
    cursor = null,
    append = false,
  }: { showSpinner?: boolean; cursor?: string | null; append?: boolean } = {}) => {
    if (append) setLoadingMore(true);
    if (showSpinner) {
      setLoading(true);
    } else {
      setRefreshing(true);
    }
    try {
      const headers: any = {};
      const token = localStorage.getItem('access_token');
      if (token) headers['Authorization'] = `Bearer ${token}`;
      if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;

      const params = new URLSearchParams();
      params.set('include_all_my_orgs', 'true');
      // Platform-admin opt-in: cross every tenant. Backend ignores it for
      // non-superusers, but only send it when actually toggled on.
      if (allTenants && isSuperuser) {
        params.set('all_tenants', 'true');
      }
      params.set('limit', '50');
      if (cursor) params.set('cursor', cursor);
      if (selectedTags.length > 0) {
        params.set('tags', selectedTags.map(encodeURIComponent).join(','));
      }
      const sessionsUrl = `${config.apiBaseUrl}/api/simple/recording-sessions?${params.toString()}`;
      const [serverRes, localList, attachCountsRes] = await Promise.allSettled([
        fetch(sessionsUrl, { headers }),
        listLocalSessions(),
        // Per-session attachment count for the paperclip icon. Org-scoped
        // by the active-org header; safe to skip on failure (the rest of
        // the card still renders, the icon just doesn't show).
        fetch(`${config.apiBaseUrl}/api/simple/recording-sessions-attachment-counts`, { headers }),
      ]);

      if (attachCountsRes.status === 'fulfilled' && attachCountsRes.value.ok) {
        try {
          const counts = await attachCountsRes.value.json();
          if (counts && typeof counts === 'object') {
            setAttachmentCounts(counts as Record<string, number>);
          }
        } catch {
          // ignore malformed payload — non-critical
        }
      }

      let serverSessions: Session[] = [];
      let serverOk = false;
      if (serverRes.status === 'fulfilled' && serverRes.value.ok) {
        serverOk = true;
        const data = await serverRes.value.json();
        serverSessions = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
        setNextCursor(Array.isArray(data) ? null : (data?.next_cursor ?? null));
        setServerError(null);
      } else if (serverRes.status === 'fulfilled') {
        console.error('Failed to fetch sessions:', serverRes.value.status, serverRes.value.statusText);
        setServerError("Couldn't refresh sessions.");
      } else {
        console.error('Failed to fetch sessions:', serverRes.reason);
        setServerError("Couldn't refresh sessions.");
      }

      const localSessions: Session[] =
        localList.status === 'fulfilled'
          ? localList.value.map(localSessionToSession)
          : [];

      // Merge: server + local, deduped by id (local ids are
      // `local-` prefixed so a clash is structurally impossible, but
      // we still de-dupe defensively).
      //
      // TRUST: on a FAILED server fetch, keep the last good list (current
      // `sessions`) instead of collapsing to local-only — a transient outage
      // must not make existing meetings look deleted. On success the server
      // payload is authoritative (prior only when appending a page).
      const seen = new Set<string>();
      const merged: Session[] = [];
      const base = serverOk ? (append ? sessions : []) : sessions;
      for (const s of [...base, ...serverSessions, ...localSessions]) {
        if (seen.has(s.id)) continue;
        seen.add(s.id);
        merged.push(s);
      }
      merged.sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      );
      setSessions(merged);
    } catch (error) {
      console.error('Failed to fetch sessions:', error);
    } finally {
      setHasFetchedOnce(true);
      if (showSpinner) {
        setLoading(false);
      } else {
        setRefreshing(false);
      }
      setLoadingMore(false);
    }
  };

  const filterSessions = () => {
    console.log('Filtering sessions. Total sessions:', sessions.length);
    
    // Use server-side search results when a search query is active
    let filtered = (searchQuery && searchQuery.length >= 2 && searchResults !== null)
      ? [...searchResults]
      : [...sessions];

    // Client-side name/title filter for short queries (< 2 chars)
    if (searchQuery && searchQuery.length < 2) {
      filtered = filtered.filter(session =>
        session.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        session.title?.toLowerCase().includes(searchQuery.toLowerCase()) ||
        session.description?.toLowerCase().includes(searchQuery.toLowerCase())
      );
    }

    // Status filter
    if (statusFilter !== 'all') {
      filtered = filtered.filter(session => session.status === statusFilter);
    }

    // Type filter
    if (typeFilter !== 'all') {
      filtered = filtered.filter(session => session.session_type === typeFilter);
    }

    // Source filter: local-only (is_local=true) vs synced (server).
    if (sourceFilter === 'local') {
      filtered = filtered.filter((session) => session.is_local === true);
    } else if (sourceFilter === 'synced') {
      filtered = filtered.filter((session) => session.is_local !== true);
    }

    if (orgFilter !== 'all') {
      filtered = filtered.filter((session) => session.organization_id === orgFilter);
    }

    // Folder view: All sessions keeps the existing flat behavior.
    // Ungrouped means "no tags"; specific folder names are an AND
    // filter that can still be combined with the chip filter bar.
    if (selectedFolder === UNGROUPED_FOLDER) {
      filtered = filtered.filter((session) => (session.tags || []).length === 0);
    } else if (selectedFolder !== 'all') {
      filtered = filtered.filter((session) => (session.tags || []).includes(selectedFolder));
    }

    // Sort
    console.log('📊 After filtering:', filtered.length, 'sessions remain');
    filtered.sort((a, b) => {
      let aValue: any, bValue: any;

      switch (sortBy) {
        case 'name':
          aValue = (a.title || a.name || '').toLowerCase();
          bValue = (b.title || b.name || '').toLowerCase();
          break;
        case 'duration':
          aValue = a.duration || 0;
          bValue = b.duration || 0;
          break;
        case 'speakers':
          aValue = deriveSpeakerCount(a);
          bValue = deriveSpeakerCount(b);
          break;
        case 'status':
          aValue = (a.status || '').toLowerCase();
          bValue = (b.status || '').toLowerCase();
          break;
        case 'date':
        default: {
          // Sort by upload (created_at) or meeting (meeting_date with
          // created_at fallback) depending on the dateBasis toggle.
          const aRaw = effectiveSessionDate(a, dateBasis);
          const bRaw = effectiveSessionDate(b, dateBasis);
          aValue = aRaw ? new Date(aRaw.length === 10 ? aRaw + 'T00:00:00' : aRaw).getTime() : 0;
          bValue = bRaw ? new Date(bRaw.length === 10 ? bRaw + 'T00:00:00' : bRaw).getTime() : 0;
          break;
        }
      }

      if (sortOrder === 'asc') {
        return aValue < bValue ? -1 : aValue > bValue ? 1 : 0;
      } else {
        return aValue > bValue ? -1 : aValue < bValue ? 1 : 0;
      }
    });

    setFilteredSessions(filtered);
  };

  const toggleSessionSelection = useCallback((sessionId: string) => {
    setSelectedSessionIds((prev) =>
      prev.includes(sessionId) ? prev.filter((id) => id !== sessionId) : [...prev, sessionId],
    );
  }, []);

  const clearSessionSelection = useCallback(() => {
    setSelectedSessionIds([]);
  }, []);

  // Clear any selection when the page unmounts so a stale set never leaks
  // back in on remount.
  useEffect(() => () => setSelectedSessionIds([]), []);

  // Select-all that operates only on the ids actually visible (post column
  // + tag + search filters). When selectAll is true it unions the visible
  // ids into the current selection (so selections made under a different
  // filter survive); when false it removes exactly the visible ids.
  const toggleSelectVisible = useCallback((visibleIds: string[], selectAll: boolean) => {
    setSelectedSessionIds((prev) => {
      if (selectAll) {
        return Array.from(new Set([...prev, ...visibleIds]));
      }
      const visibleSet = new Set(visibleIds);
      return prev.filter((id) => !visibleSet.has(id));
    });
  }, []);

  // Bulk delete. Local-only (IndexedDB) sessions can't hit the server
  // endpoint, so split them out and delete locally; everything else POSTs
  // to /api/simple/recording-sessions/bulk-delete and we reconcile the { deleted, failed }
  // result. Successfully-removed ids are spliced out of the list in place
  // (no full refetch) and the selection is cleared.
  const bulkDeleteSelected = useCallback(async () => {
    const ids = [...selectedSessionIds];
    if (ids.length === 0) return;
    if (!(await showConfirm(
      ids.length === 1
        ? 'Delete the selected session? This cannot be undone.'
        : `Delete ${ids.length} selected sessions? This cannot be undone.`,
      { title: ids.length === 1 ? 'Delete session' : 'Delete sessions', confirmLabel: 'Delete' },
    ))) {
      return;
    }

    setBulkDeleting(true);
    try {
      const localIds = ids.filter((id) => isLocalSessionId(id));
      const serverIds = ids.filter((id) => !isLocalSessionId(id));

      const deletedIds: string[] = [];
      const failures: Array<{ id: string; reason: string }> = [];

      // Local sessions first — best-effort, one IndexedDB delete each.
      await Promise.all(localIds.map(async (id) => {
        try {
          await deleteLocalSession(id);
          deletedIds.push(id);
        } catch (err) {
          failures.push({ id, reason: err instanceof Error ? err.message : 'local delete failed' });
        }
      }));

      // Server sessions via the new bulk endpoint.
      if (serverIds.length > 0) {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('access_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
        if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;

        const res = await fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/bulk-delete`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ session_ids: serverIds }),
        });

        if (!res.ok) {
          // Whole call failed — every server id is a failure.
          let detail = `${res.status} ${res.statusText}`;
          try {
            const errBody = await res.json();
            if (errBody?.detail) detail = String(errBody.detail);
          } catch { /* non-JSON error body */ }
          for (const id of serverIds) failures.push({ id, reason: detail });
        } else {
          const data = await res.json().catch(() => ({}));
          const failedList: Array<{ id?: string | number; reason?: string }> = Array.isArray(data?.failed)
            ? data.failed
            : [];
          const failedIdSet = new Set(failedList.map((f) => String(f.id)));
          for (const f of failedList) {
            failures.push({ id: String(f.id), reason: f.reason || 'delete failed' });
          }
          // Anything we asked to delete that isn't in `failed` succeeded.
          for (const id of serverIds) {
            if (!failedIdSet.has(id)) deletedIds.push(id);
          }
        }
      }

      // Splice the confirmed-deleted ids out of the list in place.
      if (deletedIds.length > 0) {
        const deletedSet = new Set(deletedIds);
        setSessions((prev) => prev.filter((s) => !deletedSet.has(s.id)));
      }
      // Drop deleted ids from the selection; keep any that failed so the
      // user can retry without re-selecting them.
      const stillSelectedSet = new Set(deletedIds);
      setSelectedSessionIds((prev) => prev.filter((id) => !stillSelectedSet.has(id)));

      // Surface the outcome.
      if (failures.length === 0) {
        showToast.success(
          deletedIds.length === 1
            ? 'Session deleted.'
            : `${deletedIds.length} sessions deleted.`,
        );
      } else if (deletedIds.length === 0) {
        showToast.error(
          failures.length === 1
            ? 'Failed to delete the session.'
            : `Failed to delete ${failures.length} sessions.`,
        );
      } else {
        showToast.warning(
          `${deletedIds.length} deleted, ${failures.length} failed.`,
        );
      }
      if (failures.length > 0) {
        console.warn('Bulk delete partial failures:', failures);
      }
    } catch (error) {
      console.error('Bulk delete failed:', error);
      showToast.error('Bulk delete failed. Please try again.');
    } finally {
      setBulkDeleting(false);
    }
  }, [activeOrganization?.slug, selectedSessionIds, setSessions]);

  // Bulk export. Local-only (IndexedDB) sessions have no server-side record
  // to bundle, so they're skipped — only server ids are POSTed to
  // /api/simple/recording-sessions/bulk-export, which streams back a ZIP.
  // We read the response as a blob and trigger a browser download, preferring
  // the Content-Disposition filename. The selection is intentionally KEPT so
  // the user can immediately follow up with another action on the same set.
  const bulkExportSelected = useCallback(async () => {
    const ids = [...selectedSessionIds];
    if (ids.length === 0) return;

    const serverIds = ids.filter((id) => !isLocalSessionId(id));
    const skippedLocal = ids.length - serverIds.length;
    if (serverIds.length === 0) {
      showToast.error('Local-only sessions can’t be exported from the server.');
      return;
    }

    setBulkExporting(true);
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      const token = localStorage.getItem('access_token');
      if (token) headers['Authorization'] = `Bearer ${token}`;
      if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;

      const res = await fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/bulk-export`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ session_ids: serverIds }),
      });

      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const errBody = await res.json();
          if (errBody?.detail) detail = String(errBody.detail);
        } catch { /* non-JSON / binary error body */ }
        showToast.error(`Export failed: ${detail}`);
        return;
      }

      // Prefer the server-supplied filename from Content-Disposition.
      let filename = 'meeting-ops-export.zip';
      const disposition = res.headers.get('Content-Disposition');
      if (disposition) {
        const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        const quotedMatch = disposition.match(/filename="?([^";]+)"?/i);
        if (utf8Match?.[1]) {
          try { filename = decodeURIComponent(utf8Match[1]); } catch { /* keep default */ }
        } else if (quotedMatch?.[1]) {
          filename = quotedMatch[1].trim();
        }
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        // Revoke on the next tick so the download has a chance to start.
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }

      showToast.success(
        skippedLocal > 0
          ? `Exported ${serverIds.length} session${serverIds.length === 1 ? '' : 's'} (${skippedLocal} local-only skipped).`
          : `Exported ${serverIds.length} session${serverIds.length === 1 ? '' : 's'}.`,
      );
      // Selection intentionally preserved (no clearSessionSelection here).
    } catch (error) {
      console.error('Bulk export failed:', error);
      showToast.error('Bulk export failed. Please try again.');
    } finally {
      setBulkExporting(false);
    }
  }, [activeOrganization?.slug, selectedSessionIds]);

  const selectFolder = useCallback((folder: string) => {
    setGroupingMode('folders');
    setSelectedFolder(folder);
    if (folder === 'all' || folder === UNGROUPED_FOLDER) {
      setSelectedTags([]);
      return;
    }
    setSelectedTags([folder]);
  }, []);

  const mutateFolderTag = useCallback(async (
    sessionIds: string[],
    tag: string,
    action: 'add' | 'remove',
  ) => {
    const cleanedTag = tag.trim();
    if (!cleanedTag || sessionIds.length === 0) return;

    setFolderMutationBusy(true);
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      const token = localStorage.getItem('access_token');
      if (token) headers['Authorization'] = `Bearer ${token}`;
      if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;

      await Promise.all(sessionIds.map(async (sessionId) => {
        const session = sessions.find((item) => item.id === sessionId);
        if (session?.is_local || isLocalSessionId(sessionId)) {
          const current = [...(session?.tags || [])];
          const next = action === 'add'
            ? Array.from(new Set([...current, cleanedTag]))
            : current.filter((value) => value !== cleanedTag);
          await updateLocalSessionMeta(sessionId, { tags: next });
          return;
        }

        const url = `${config.apiBaseUrl}/api/simple/recording-sessions/${encodeURIComponent(sessionId)}/tags`;
        if (action === 'add') {
          const res = await fetch(url, {
            method: 'POST',
            headers,
            body: JSON.stringify({ tag: cleanedTag }),
          });
          if (!res.ok) throw new Error(`Failed to add folder "${cleanedTag}" to ${sessionId}`);
        } else {
          const res = await fetch(`${url}/${encodeURIComponent(cleanedTag)}`, {
            method: 'DELETE',
            headers,
          });
          if (!res.ok) throw new Error(`Failed to remove folder "${cleanedTag}" from ${sessionId}`);
        }
      }));

      await Promise.all([
        fetchSessions({ showSpinner: false }),
        refreshOrgTags(),
      ]);
    } catch (error) {
      console.error('Failed to mutate folder tags:', error);
      showToast.error(error instanceof Error ? error.message : 'Failed to update folder tags');
    } finally {
      setFolderMutationBusy(false);
    }
  }, [activeOrganization?.slug, refreshOrgTags, sessions]);

  const addSelectedSessionsToFolder = useCallback(async (tag: string) => {
    await mutateFolderTag(selectedSessionIds, tag, 'add');
    clearSessionSelection();
  }, [clearSessionSelection, mutateFolderTag, selectedSessionIds]);

  const removeSessionFromFolder = useCallback(async (sessionId: string, tag: string) => {
    await mutateFolderTag([sessionId], tag, 'remove');
  }, [mutateFolderTag]);

  const handleNewFolder = useCallback(() => {
    const value = window.prompt('New folder name');
    if (!value) return;
    const tag = value.trim();
    if (!tag) return;
    if (selectedSessionIds.length > 0) {
      void addSelectedSessionsToFolder(tag);
      setSelectedFolder(tag);
      setGroupingMode('folders');
      setSelectedTags([tag]);
      return;
    }
    setGroupingMode('folders');
    setSelectedFolder(tag);
    setSelectedTags([tag]);
  }, [addSelectedSessionsToFolder, selectedSessionIds.length, setSelectedTags]);

  const handleSessionCreated = (sessionId: string) => {
    setShowCreator(false);
    fetchSessions();
    // Navigate to the new session
    navigate(`/sessions/${sessionId}`);
  };

  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [uploadOptions, setUploadOptions] = useState<TranscriptionOptions>(
    DEFAULT_TRANSCRIPTION_OPTIONS,
  );
  // When true, the *next* file picked via the file input opens the
  // options modal instead of auto-starting. The big upload tile leaves
  // this false (fastest path); the smaller "with options" link flips
  // it true just before invoking the picker.
  const [pickerWithOptions, setPickerWithOptions] = useState(false);

  const handleUploadPicked = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = '';
    if (files.length === 0) return;
    if (pickerWithOptions) {
      // Explicit "with options" path: route through the modal so the
      // user can pick STT engine, diarization sensitivity, summary
      // template, etc. before the run begins.
      setPickerWithOptions(false);
      setPendingFiles(files);
      setUploadOptions(DEFAULT_TRANSCRIPTION_OPTIONS);
      return;
    }
    // Default fast path: kick off processing with the org defaults.
    // Matches drag-and-drop behaviour for consistency. The Re-process
    // button on SessionDetails covers tweaking options after the fact.
    void startUploads(files, { action: 'transcribe' });
  };

  const confirmUpload = () => {
    if (pendingFiles.length === 0) return;
    const opts = serializeTranscriptionOptions(uploadOptions);
    void startUploads(pendingFiles, {
      action: 'transcribe',
      ...(opts ? { transcriptionOptions: opts } : {}),
    });
    setPendingFiles([]);
  };

  const cancelUpload = () => {
    setPendingFiles([]);
  };



  const deleteSession = async (sessionId: string) => {
    if (!(await showConfirm(
      'Are you sure you want to delete this session?',
      { title: 'Delete session', confirmLabel: 'Delete' },
    ))) return;

    if (isLocalSessionId(sessionId)) {
      try {
        await deleteLocalSession(sessionId);
        fetchSessions();
      } catch (error) {
        console.error('Failed to delete local session:', error);
        showToast.error('Failed to delete local session');
      }
      return;
    }

    try {
      const response = await fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });

      if (response.ok) {
        fetchSessions();
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
      showToast.error('Failed to delete session');
    }
  };

  const downloadTranscript = async (session: Session) => {
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/${session.id}/transcript`, {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });

      if (response.ok) {
        const data = await response.json();
        const blob = new Blob([data.transcript || 'No transcript available'], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${session.name}_transcript.txt`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      console.error('Failed to download transcript:', error);
    }
  };

  const downloadAudio = async (session: Session) => {
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/${session.id}/audio`, {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        }
      });

      if (response.ok) {
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${session.name}_audio.wav`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      console.error('Failed to download audio:', error);
    }
  };

  const downloadSummary = (session: Session) => {
    if (!session.summary) {
      showToast.info('No summary available for download');
      return;
    }
    
    let content = `MEETING SUMMARY\n`;
    content += `===============\n\n`;
    content += `Meeting: ${session.title || session.name}\n`;
    content += `Date: ${new Date(session.created_at).toLocaleDateString()}\n`;
    content += `Duration: ${session.duration ? formatDuration(session.duration) : 'N/A'}\n\n`;
    
    if (session.summary.analysis?.executive) {
      content += `EXECUTIVE SUMMARY\n`;
      content += `-----------------\n`;
      content += `${session.summary.analysis.executive}\n\n`;
    }
    
    if (session.summary.analysis?.bullets && session.summary.analysis.bullets.length > 0) {
      content += `KEY DISCUSSION POINTS\n`;
      content += `---------------------\n`;
      session.summary.analysis.bullets.forEach((bullet: string) => {
        content += `• ${bullet}\n`;
      });
      content += '\n';
    }
    
    if (session.summary.analysis?.decisions && session.summary.analysis.decisions.length > 0) {
      content += `IMPORTANT DECISIONS\n`;
      content += `-------------------\n`;
      session.summary.analysis.decisions.forEach((decision: string) => {
        content += `✓ ${decision}\n`;
      });
      content += '\n';
    }
    
    if (session.summary.analysis?.actions && session.summary.analysis.actions.length > 0) {
      content += `ACTION ITEMS\n`;
      content += `------------\n`;
      session.summary.analysis.actions.forEach((action: any) => {
        content += `□ ${action.action}\n`;
        if (action.owner) content += `  Owner: ${action.owner}\n`;
        if (action.priority) content += `  Priority: ${action.priority}\n`;
        content += '\n';
      });
    }
    
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${(session.title || session.name).replace(/\s+/g, '_')}_summary.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'active':
      case 'recording': return 'text-red-400 bg-red-500/10 border-red-500';
      case 'completed': return 'text-green-400 bg-green-500/10 border-green-500';
      case 'processing': return 'text-yellow-400 bg-yellow-500/10 border-yellow-500';
      case 'failed': return 'text-red-400 bg-red-500/10 border-red-500/50';
      default: return 'text-gray-400 bg-gray-500/10 border-gray-500';
    }
  };

  const getStatusLabel = (status: string) => {
    if (status === 'active') return 'Recording';
    if (status === 'failed') return 'Failed';
    return status.charAt(0).toUpperCase() + status.slice(1);
  };

  const formatDuration = (seconds?: number) => {
    if (!seconds) return '--:--';
    const mins = Math.floor(seconds / 60);
    // Durations arrive as fractional seconds — floor before formatting or
    // the card renders "93:7.69320499999958".
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const activeFolderLabel = selectedFolder === 'all'
    ? 'All sessions'
    : selectedFolder === UNGROUPED_FOLDER
      ? 'Ungrouped'
      : selectedFolder;
  const activeFolderCount = selectedFolder === 'all'
    ? orgTagStats.totalSessions
    : selectedFolder === UNGROUPED_FOLDER
      ? orgTagStats.untaggedSessions
      : (tagCountMap[selectedFolder] || 0);

  return (
    <>
      <MobileSessionsList
        sessions={displayedSessions as any}
        loading={loading}
        refreshing={refreshing || isSearching}
        searchQuery={searchQuery}
        onSearchChange={(value) => {
          setSearchQuery(value);
          if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
          searchTimerRef.current = setTimeout(() => {
            performSearch(value);
          }, 300);
        }}
        folderSummaries={orgTagSummaries}
        totalSessions={orgTagStats.totalSessions}
        untaggedSessions={orgTagStats.untaggedSessions}
        selectedFolder={selectedFolder}
        onSelectFolder={selectFolder}
        onNewFolder={handleNewFolder}
        orgTags={orgTags}
        selectedTags={selectedTags}
        onToggleTag={(tag) => {
          setSelectedTags((prev) =>
            prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]
          );
        }}
        onClearTags={() => setSelectedTags([])}
      />
      <div className="hidden md:block min-h-screen bg-gray-950 text-white">
      {showCreator && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-6">
          <div className="w-full max-w-4xl max-h-[90vh] overflow-y-auto">
            <SessionCreator
              onClose={() => setShowCreator(false)}
              onSessionCreated={handleSessionCreated}
            />
          </div>
        </div>
      )}

      <div className="p-6">
        {/* Free-tier upgrade banner. Renders only for free users; dismissible
            for 7 days via localStorage. See components/UpgradeBanner.tsx. */}
        <UpgradeBanner />
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-3xl font-bold text-white">Sessions</h1>
            <p className="text-gray-400 mt-1">Manage your meeting recordings and transcriptions</p>
          </div>

          <div className="flex items-center gap-3">
            {/* v3.23.0: Upload Audio/Video only renders for Pro+ tiers
                (audio_upload tier flag). Free + Basic users stay
                browser-only for audio; the New Session path still
                works for them. */}
            {canUploadAudio && (
              <button
                onClick={() => uploadInputRef.current?.click()}
                className="flex items-center gap-2 rounded-lg bg-cyan-600 px-5 py-3 text-white transition-colors hover:bg-cyan-500"
              >
                <Upload className="w-5 h-5" />
                Upload Audio/Video
              </button>
            )}
            <button
              onClick={() => setShowCreator(true)}
              className="flex items-center gap-2 px-6 py-3 bg-purple-600 hover:bg-purple-700 text-white rounded-lg transition-colors"
            >
              <Plus className="w-5 h-5" />
              New Session
            </button>
          </div>
          {canUploadAudio && (
            <input
              ref={uploadInputRef}
              type="file"
              multiple
              accept="audio/*,video/*,.mp3,.wav,.m4a,.ogg,.flac,.opus,.aac,.mp4,.mov,.mkv,.webm,.avi"
              onChange={handleUploadPicked}
              className="hidden"
            />
          )}
        </div>

        {/* Controls */}
        <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 mb-6">
          <div className="flex items-center gap-4">
            {/* Search */}
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                placeholder="Search sessions (keyword + semantic)..."
                value={searchQuery}
                onChange={handleSearchChange}
                className="w-full pl-10 pr-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:border-purple-500"
              />
            </div>

            {/* Ask AI - Cross-meeting RAG */}
            {searchQuery && searchQuery.length >= 2 && (
              <button
                onClick={askAiAboutMeetings}
                disabled={ragLoading}
                className="flex items-center gap-2 px-4 py-2 bg-fuchsia-600 hover:bg-fuchsia-700 disabled:opacity-50 text-white rounded-lg transition-colors whitespace-nowrap"
                title="Ask AI about all meetings"
              >
                {ragLoading ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <MessageSquare className="w-4 h-4" />
                )}
                Ask AI
              </button>
            )}

            {/* Date Basis Toggle — controls whether the date column +
                'date' sort use the server upload time or the
                user-editable meeting_date. */}
            <div className="flex items-center bg-gray-800 rounded-lg" title="Sort sessions by upload date or meeting date">
              <button
                onClick={() => setDateBasis('upload')}
                className={`px-3 py-2 text-xs rounded-l-lg transition-colors ${dateBasis === 'upload' ? 'bg-purple-600 text-white' : 'text-gray-400 hover:text-gray-200'}`}
                aria-pressed={dateBasis === 'upload'}
              >
                Uploaded
              </button>
              <button
                onClick={() => setDateBasis('meeting')}
                className={`px-3 py-2 text-xs rounded-r-lg transition-colors ${dateBasis === 'meeting' ? 'bg-purple-600 text-white' : 'text-gray-400 hover:text-gray-200'}`}
                aria-pressed={dateBasis === 'meeting'}
              >
                Meeting date
              </button>
            </div>

            {/* View Mode Toggle */}
            <div className="flex items-center bg-gray-800 rounded-lg">
              <button
                onClick={() => setViewMode('grid')}
                className={`p-2 rounded-l-lg ${viewMode === 'grid' ? 'bg-purple-600 text-white' : 'text-gray-400'}`}
              >
                <Grid className="w-4 h-4" />
              </button>
              <button
                onClick={() => setViewMode('list')}
                className={`p-2 rounded-none ${viewMode === 'list' ? 'bg-purple-600 text-white' : 'text-gray-400'}`}
              >
                <List className="w-4 h-4" />
              </button>
              <button
                onClick={() => setGroupingMode(groupingMode === 'folders' ? 'flat' : 'folders')}
                className={`p-2 rounded-r-lg border-l border-gray-700 ${groupingMode === 'folders' ? 'bg-purple-600 text-white' : 'text-gray-400'}`}
                title="Show folders"
              >
                <FolderOpen className="w-4 h-4" />
              </button>
            </div>

            {/* Select all / none — one click, works in BOTH grid and list
                views (the list-view header checkbox only covers its own
                column-filtered subset; this covers the whole filtered list). */}
            {filteredSessions.length > 0 && (
              <div
                className="flex items-center bg-gray-800 rounded-lg text-xs"
                title="Select or clear every session in this list"
              >
                <button
                  type="button"
                  onClick={() => toggleSelectVisible(displayedSessions.map((s) => s.id), true)}
                  className="px-3 py-2 rounded-l-lg text-gray-300 hover:text-white transition-colors"
                >
                  Select all ({displayedSessions.length})
                </button>
                <button
                  type="button"
                  onClick={clearSessionSelection}
                  disabled={selectedSessionIds.length === 0}
                  className="px-3 py-2 rounded-r-lg border-l border-gray-700 text-gray-300 hover:text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  None
                </button>
              </div>
            )}

            {/* Refresh + auto-refresh */}
            <div className="flex items-center bg-gray-800 rounded-lg">
              <button
                onClick={() => fetchSessions({ showSpinner: false })}
                disabled={refreshing || loading}
                title="Refresh now"
                aria-label="Refresh sessions"
                className="p-2 rounded-l-lg text-gray-300 hover:text-white disabled:opacity-50"
              >
                <RefreshCw className={`w-4 h-4 ${(refreshing || loading) ? 'animate-spin' : ''}`} />
              </button>
              <button
                onClick={() => setAutoRefresh((v) => !v)}
                title={autoRefresh ? 'Auto-refresh on — click to pause' : 'Auto-refresh off — click to enable'}
                aria-pressed={autoRefresh}
                className={`px-3 py-2 text-xs rounded-r-lg border-l border-gray-700 transition-colors ${autoRefresh ? 'bg-purple-600 text-white' : 'text-gray-400 hover:text-gray-200'}`}
              >
                Auto
              </button>
            </div>

            {/* Filters */}
            <button
              onClick={() => setShowFilters(!showFilters)}
              className="flex items-center gap-2 px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg transition-colors"
            >
              <Filter className="w-4 h-4" />
              Filters
              <ChevronDown className={`w-4 h-4 transition-transform ${showFilters ? 'rotate-180' : ''}`} />
            </button>
          </div>

        {/* Extended Filters */}
        {showFilters && (
          <div className="mt-4 pt-4 border-t border-gray-800">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value)}
                  className="px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-purple-500"
                >
                  <option value="all">All Status</option>
                  <option value="idle">Idle</option>
                  <option value="active">Recording</option>
                  <option value="processing">Processing</option>
                  <option value="completed">Completed</option>
                  <option value="failed">Failed</option>
                </select>

                <select
                  value={typeFilter}
                  onChange={(e) => setTypeFilter(e.target.value)}
                  className="px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-purple-500"
                >
                  <option value="all">All Types</option>
                  <option value="live">Live Recording</option>
                  <option value="upload">Uploaded Audio</option>
                </select>

                <select
                  value={sourceFilter}
                  onChange={(e) => setSourceFilter(e.target.value as 'all' | 'local' | 'synced')}
                  className="px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-purple-500"
                  title="Filter by storage tier"
                >
                  <option value="all">All Storage</option>
                  <option value="local">Local only</option>
                  <option value="synced">Synced only</option>
                </select>

                <select
                  value={sortBy}
                  onChange={(e) => setSort(e.target.value as SortKey, sortOrder)}
                  className="px-3 py-2 bg-gray-800 border border-gray-700 rounded text-white focus:outline-none focus:border-purple-500"
                >
                  <option value="date">Sort by Date</option>
                  <option value="name">Sort by Name</option>
                  <option value="duration">Sort by Duration</option>
                  <option value="speakers">Sort by Speakers</option>
                  <option value="status">Sort by Status</option>
                </select>

                <button
                  onClick={() => setSort(sortBy, sortOrder === 'asc' ? 'desc' : 'asc')}
                  className="flex items-center justify-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded text-white transition-colors"
                >
                  {sortOrder === 'asc' ? <SortAsc className="w-4 h-4" /> : <SortDesc className="w-4 h-4" />}
                  {sortOrder === 'asc' ? 'Ascending' : 'Descending'}
                </button>
            </div>
          </div>
        )}
      </div>

      {groupingMode === 'folders' && (
        <div className="bg-gradient-to-br from-gray-900 to-gray-950 border border-purple-800/40 rounded-lg p-4 mb-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <FolderOpen className="w-4 h-4 text-purple-300" />
                <h2 className="text-sm font-semibold uppercase tracking-wide text-purple-200">Folders</h2>
              </div>
              <p className="mt-1 text-sm text-gray-400">
                Tags act as folders. A session can live in multiple folders.
              </p>
            </div>
            <button
              type="button"
              onClick={handleNewFolder}
              className="inline-flex items-center gap-2 rounded-lg bg-purple-600 px-3 py-2 text-sm font-medium text-white hover:bg-purple-500"
            >
              <Plus className="w-4 h-4" />
              New folder
            </button>
          </div>

          <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            <button
              type="button"
              onClick={() => selectFolder('all')}
              className={`flex items-center justify-between rounded-lg border px-3 py-2 text-left transition-colors ${
                selectedFolder === 'all'
                  ? 'border-purple-500 bg-purple-600/20 text-white'
                  : 'border-gray-700 bg-gray-900 text-gray-300 hover:bg-gray-800'
              }`}
            >
              <span>All sessions</span>
              <span className="text-xs text-gray-400">{orgTagStats.totalSessions}</span>
            </button>
            <button
              type="button"
              onClick={() => selectFolder(UNGROUPED_FOLDER)}
              className={`flex items-center justify-between rounded-lg border px-3 py-2 text-left transition-colors ${
                selectedFolder === UNGROUPED_FOLDER
                  ? 'border-purple-500 bg-purple-600/20 text-white'
                  : 'border-gray-700 bg-gray-900 text-gray-300 hover:bg-gray-800'
              }`}
            >
              <span>Ungrouped</span>
              <span className="text-xs text-gray-400">{orgTagStats.untaggedSessions}</span>
            </button>
            {orgTagSummaries.length > 0 ? (
              orgTagSummaries.map(({ tag, count }) => (
                <button
                  key={tag}
                  type="button"
                  onClick={() => selectFolder(tag)}
                  className={`flex items-center justify-between rounded-lg border px-3 py-2 text-left transition-colors ${
                    selectedFolder === tag
                      ? 'border-purple-500 bg-purple-600/20 text-white'
                      : 'border-gray-700 bg-gray-900 text-gray-300 hover:bg-gray-800'
                  }`}
                >
                  <span className="truncate pr-2">{tag}</span>
                  <span className="text-xs text-gray-400">{count}</span>
                </button>
              ))
            ) : (
              <div className="md:col-span-2 xl:col-span-2 rounded-lg border border-dashed border-gray-700 bg-gray-950/60 px-3 py-3 text-sm text-gray-500">
                No folders yet. Create one and add it to a session.
              </div>
            )}
          </div>

          {selectedSessionIds.length > 0 && (
            <div className="mt-4 flex flex-wrap items-center gap-3 rounded-lg border border-gray-800 bg-gray-950/70 px-3 py-3">
              <span className="text-sm text-gray-300">
                {selectedSessionIds.length} selected
              </span>
              <button
                type="button"
                disabled={folderMutationBusy || selectedFolder === 'all' || selectedFolder === UNGROUPED_FOLDER}
                onClick={() => void addSelectedSessionsToFolder(selectedFolder)}
                className="rounded-lg bg-purple-600 px-3 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 hover:bg-purple-500"
              >
                Add to current folder
              </button>
              {selectedFolder !== 'all' && selectedFolder !== UNGROUPED_FOLDER && (
                <button
                  type="button"
                  disabled={folderMutationBusy}
                  onClick={() => void mutateFolderTag(selectedSessionIds, selectedFolder, 'remove')}
                  className="rounded-lg border border-gray-700 px-3 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Remove from current folder
                </button>
              )}
              <button
                type="button"
                onClick={clearSessionSelection}
                className="text-sm text-gray-400 hover:text-white"
              >
                Clear selection
              </button>
            </div>
          )}

          <div className="mt-3 text-xs text-gray-500">
            Active folder: <span className="text-gray-300">{activeFolderLabel}</span>
            <span className="mx-2 text-gray-700">•</span>
            {activeFolderCount} session{activeFolderCount === 1 ? '' : 's'}
          </div>
        </div>
      )}

        {organizations.length > 1 && (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3 mb-6 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500 uppercase tracking-wide mr-1">Orgs:</span>
            <button
              type="button"
              onClick={() => setOrgFilter('all')}
              className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                orgFilter === 'all'
                  ? 'border-purple-500 bg-purple-600/20 text-purple-100'
                  : 'border-gray-700 bg-zinc-800 text-gray-300 hover:bg-gray-800'
              }`}
            >
              All orgs
            </button>
            {organizations.filter((org) => org.is_active).map((org) => (
              <button
                key={org.id}
                type="button"
                onClick={() => setOrgFilter(org.id)}
                className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                  orgFilter === org.id
                    ? 'border-purple-500 bg-purple-600/20 text-purple-100'
                    : 'border-gray-700 bg-zinc-800 text-gray-300 hover:bg-gray-800'
                }`}
              >
                {org.name}
              </button>
            ))}
          </div>
        )}

        {/* Platform-admin god-view (superusers only). Off by default — the
            normal list is scoped to the admin's own workspaces, same as any
            user. Toggling it on fetches sessions across EVERY tenant and is
            loudly indicated, because it means viewing customers' private
            meetings. The backend ignores the flag for non-superusers. */}
        {isSuperuser && (
          <div
            className={`mb-6 flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3 py-2 ${
              allTenants
                ? 'border-amber-500/60 bg-amber-500/10'
                : 'border-gray-800 bg-gray-900'
            }`}
          >
            <div className="flex items-center gap-2 text-xs">
              {allTenants ? (
                <AlertTriangle className="h-4 w-4 text-amber-400" />
              ) : (
                <LockIcon className="h-4 w-4 text-gray-500" />
              )}
              <span className="font-medium text-gray-300">Platform admin</span>
              <span className="text-gray-500">
                {allTenants
                  ? '— viewing ALL tenants’ meetings across the platform'
                  : '— viewing only your own workspaces'}
              </span>
            </div>
            <button
              type="button"
              onClick={() => { setOrgFilter('all'); setAllTenants((v) => !v); }}
              className={`rounded-md border px-3 py-1 text-xs font-medium transition-colors ${
                allTenants
                  ? 'border-amber-500 bg-amber-600/20 text-amber-100 hover:bg-amber-600/30'
                  : 'border-gray-700 bg-zinc-800 text-gray-300 hover:bg-gray-700'
              }`}
            >
              {allTenants ? 'Exit all-tenants view' : 'View all tenants'}
            </button>
          </div>
        )}

        {/* Tag filter chip palette — multi-select AND. */}
        {orgTags.length > 0 && (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-3 mb-6 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500 uppercase tracking-wide mr-1">Tags:</span>
            {orgTags.map((tag) => {
              const selected = selectedTags.includes(tag);
              return (
                <TagChip
                  key={tag}
                  tag={tag}
                  variant="filter"
                  selected={selected}
                  onClick={() => {
                    setSelectedTags((prev) =>
                      selected ? prev.filter((t) => t !== tag) : [...prev, tag]
                    );
                  }}
                />
              );
            })}
            {selectedTags.length > 0 && (
              <button
                onClick={() => setSelectedTags([])}
                className="ml-2 text-xs text-gray-400 hover:text-white underline-offset-2 hover:underline"
              >
                Clear ({selectedTags.length})
              </button>
            )}
          </div>
        )}

        {/* RAG Chat Panel (multi-turn) */}
        {showRagPanel && (
          <div className="bg-gray-900 rounded-lg border border-fuchsia-800/50 p-4 mb-6">
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center gap-2">
                <MessageSquare className="w-5 h-5 text-fuchsia-400" />
                <h3 className="text-lg font-semibold text-fuchsia-300">AI Chat</h3>
                <span className="text-xs text-gray-500">across all meetings</span>
              </div>
              <div className="flex items-center gap-2">
                {ragMessages.length > 0 && (
                  <button
                    onClick={async () => {
                      setRagMessages([]);
                      try {
                        const token = localStorage.getItem('access_token');
                        await fetch(`${config.apiBaseUrl}/api/ai-chat/rag/history`, {
                          method: 'DELETE',
                          headers: { ...(token ? { 'Authorization': `Bearer ${token}` } : {}) },
                        });
                      } catch { /* ignore */ }
                    }}
                    className="px-2 py-1 text-xs text-gray-400 hover:text-white border border-gray-700 rounded transition-colors"
                    title="Clear conversation"
                  >
                    Clear
                  </button>
                )}
                <button
                  onClick={() => setShowRagPanel(false)}
                  className="p-1 text-gray-400 hover:text-white rounded transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Conversation history */}
            <div className="max-h-96 overflow-y-auto space-y-3 mb-3">
              {ragMessages.map((msg, idx) => (
                <div key={idx}>
                  {msg.role === 'user' ? (
                    <div className="flex justify-end">
                      <div className="max-w-[85%] bg-fuchsia-700/30 border border-fuchsia-700/50 rounded-lg px-3 py-2 text-sm text-gray-200">
                        {msg.content}
                      </div>
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {msg.rewrittenQuery && (
                        <p className="text-xs text-gray-500 italic">
                          Searched for: "{msg.rewrittenQuery}"
                        </p>
                      )}
                      <div className="text-gray-200 text-sm whitespace-pre-wrap leading-relaxed">
                        {msg.content}
                      </div>
                      {msg.sources && msg.sources.length > 0 && (
                        <div className="border-t border-gray-800 pt-2 mt-2">
                          <p className="text-xs text-gray-500 mb-1.5">Sources:</p>
                          <div className="flex flex-wrap gap-2">
                            {msg.sources.map((src, sidx) => (
                              <button
                                key={sidx}
                                onClick={() => navigate(`/sessions/${src.session_id}`)}
                                title={`${Math.round(src.score * 100)}% relevance match`}
                                className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs rounded-lg border border-gray-700 transition-colors"
                              >
                                <FileText className="w-3 h-3 text-fuchsia-400" />
                                {src.title || 'Untitled meeting'}
                                <span className="text-gray-500" aria-label="relevance match">({Math.round(src.score * 100)}%)</span>
                              </button>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
              {ragLoading && (
                <div className="flex items-center gap-3 py-2">
                  <Loader2 className="w-4 h-4 text-fuchsia-400 animate-spin" />
                  <span className="text-gray-400 text-sm">Searching meetings and generating answer...</span>
                </div>
              )}
              <div ref={ragEndRef} />
            </div>

            {/* Follow-up input — also shown while the first answer is in flight
                (ragMessages is briefly empty during that window) so the box never
                vanishes mid-ask; it's disabled during loading. */}
            {(ragMessages.length > 0 || ragLoading) && (
              <div className="flex gap-2 border-t border-gray-800 pt-3">
                <input
                  type="text"
                  value={ragFollowUp}
                  onChange={(e) => setRagFollowUp(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleRagFollowUp(); } }}
                  placeholder="Ask a follow-up question..."
                  disabled={ragLoading}
                  className="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm placeholder-gray-400 focus:outline-none focus:border-fuchsia-500 disabled:opacity-50"
                />
                <button
                  onClick={handleRagFollowUp}
                  disabled={ragLoading || !ragFollowUp.trim()}
                  className="px-4 py-2 bg-fuchsia-600 hover:bg-fuchsia-700 disabled:opacity-50 text-white rounded-lg text-sm transition-colors"
                >
                  Send
                </button>
              </div>
            )}
          </div>
        )}

        {/* Results count + page-size control (caps how many cards render at
            once; 'All' renders everything the server returned). */}
        {filteredSessions.length > 0 && (
          <div className="mb-3 flex items-center justify-between text-xs text-gray-400">
            <span>
              Showing {displayedSessions.length} of {filteredSessions.length}
              {loading && <span className="ml-2 text-gray-500">· Refreshing…</span>}
            </span>
            <label className="flex items-center gap-2">
              <span className="hidden sm:inline">Per page</span>
              <select
                value={String(pageSize)}
                onChange={(e) =>
                  setPageSize(e.target.value === 'all' ? 'all' : Number(e.target.value))
                }
                className="bg-gray-800 border border-gray-700 rounded-md px-2 py-1 text-gray-200 focus:outline-none focus:border-fuchsia-500"
                title="How many sessions to show at once"
              >
                <option value="12">12</option>
                <option value="25">25</option>
                <option value="50">50</option>
                <option value="all">All</option>
              </select>
            </label>
          </div>
        )}

        {/* Couldn't-refresh banner: a fetch failed but we kept the cached list. */}
        {serverError && filteredSessions.length > 0 && (
          <div className="mb-4 px-4 py-2 rounded-lg bg-amber-900/30 border border-amber-700/50 text-amber-200 text-sm flex items-center justify-between gap-3">
            <span>{serverError} Showing your last loaded sessions.</span>
            <button
              onClick={() => fetchSessions({ showSpinner: false })}
              className="underline hover:text-amber-100 shrink-0"
            >
              Retry
            </button>
          </div>
        )}

        {/* Sessions */}
        {/* TRUST: only show the skeleton when there is nothing to display yet.
            A refresh over a populated list keeps the existing cards visible
            (the refresh button spins as the loading affordance) so meetings
            never briefly vanish while `loading` is true. */}
        {filteredSessions.length === 0 && (loading || !hasFetchedOnce) ? (
          <SkeletonSessionGrid count={6} />
        ) : filteredSessions.length === 0 ? (
          serverError ? (
            // TRUST: a failed load must not read as "no meetings" / data loss.
            <div className="text-center py-12">
              <FolderOpen className="w-16 h-16 text-amber-500/70 mx-auto mb-4" />
              <h3 className="text-xl font-semibold text-gray-300 mb-2">Couldn't load sessions</h3>
              <p className="text-gray-500 mb-6">
                {serverError} Your meetings aren't gone — we just couldn't reach the server.
              </p>
              <button
                onClick={() => fetchSessions({ showSpinner: true })}
                className="flex items-center gap-2 px-6 py-3 bg-purple-600 hover:bg-purple-700 text-white rounded-lg transition-colors mx-auto"
              >
                Retry
              </button>
            </div>
          ) : (
          <div className="text-center py-12">
            <FolderOpen className="w-16 h-16 text-gray-600 mx-auto mb-4" />
            <h3 className="text-xl font-semibold text-gray-300 mb-2">No Sessions Found</h3>
            <p className="text-gray-500 mb-6">
              {searchQuery || statusFilter !== 'all' || typeFilter !== 'all' || orgFilter !== 'all'
                ? 'Try adjusting your filters or search query.'
                : 'Create your first session to get started.'
              }
            </p>
            {!searchQuery && statusFilter === 'all' && typeFilter === 'all' && orgFilter === 'all' && (
              <button
                onClick={() => setShowCreator(true)}
                className="flex items-center gap-2 px-6 py-3 bg-purple-600 hover:bg-purple-700 text-white rounded-lg transition-colors mx-auto"
              >
                <Plus className="w-5 h-5" />
                Create First Session
              </button>
            )}
          </div>
          )
        ) : (
          viewMode === 'grid' ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {/* v3.19 (audit §7 a11y). Was a `<button>` with a nested
                `<span role="button">` — invalid HTML (interactive in
                interactive) which screen readers + AT can't parse.
                Refactored to a single outer card with two real sibling
                buttons inside, both keyboard + AT addressable. */}
            <div className="flex min-h-64 flex-col items-center justify-center rounded-lg border border-dashed border-cyan-500/70 bg-gray-900 p-6 text-center transition-colors hover:border-cyan-300 hover:bg-gray-900/80">
              <button
                type="button"
                onClick={() => uploadInputRef.current?.click()}
                className="flex w-full flex-col items-center justify-center rounded-md text-center focus:outline-none focus:ring-2 focus:ring-cyan-400"
              >
                <Upload className="mb-3 h-10 w-10 text-cyan-300" />
                <span className="text-lg font-semibold text-white">Upload Audio/Video</span>
                <span className="mt-2 max-w-xs text-sm text-gray-400">Drop files anywhere or pick recordings to transcribe as meetings.</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  setPickerWithOptions(true);
                  uploadInputRef.current?.click();
                }}
                className="mt-3 rounded text-xs text-cyan-300/80 hover:text-cyan-200 underline-offset-2 hover:underline focus:outline-none focus:ring-2 focus:ring-cyan-400"
              >
                Upload with options…
              </button>
            </div>
            {displayedSessions.map((session, _i) => (
              <React.Fragment key={session.id}>
                {sessionDividerLabel(displayedSessions, _i, sortBy, dateBasis) && (
                  <div className="col-span-full mt-3 mb-1 border-t border-gray-800 pt-3 text-xs font-semibold uppercase tracking-wide text-gray-500">
                    {sessionDividerLabel(displayedSessions, _i, sortBy, dateBasis)}
                  </div>
                )}
              <div
                className={`bg-gray-900 border rounded-lg hover:border-gray-700 transition-all p-6 ${
                  selectedSessionIds.includes(session.id)
                    ? 'border-purple-500/70 ring-1 ring-purple-500/40'
                    : 'border-gray-800'
                }`}
              >
                <div className="space-y-4">
                  {/* Header */}
                  <div className="flex items-start justify-between gap-2">
                    <input
                      type="checkbox"
                      checked={selectedSessionIds.includes(session.id)}
                      onChange={() => toggleSessionSelection(session.id)}
                      className="mt-1 h-4 w-4 shrink-0 rounded border-gray-600 bg-gray-900 text-purple-500 focus:ring-purple-500"
                      aria-label="Select session"
                    />
                    <div className="flex-1">
                      <h3 className="font-semibold text-white text-lg break-words line-clamp-2 leading-tight flex items-center gap-2">
                        {session.is_local && (
                          <span
                            title="Local-only session (privacy mode). Stored only in this browser."
                            className="inline-flex items-center rounded-full border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-200"
                          >
                            <LockIcon className="w-3 h-3 mr-1" />
                            Local
                          </span>
                        )}
                        <span>{session.title || session.name}</span>
                        {(() => {
                          // Paperclip icon when the session has attachments
                          // (Granola notes, external transcripts, etc.).
                          // Counts are pre-fetched into attachmentCounts keyed
                          // by integer pk-as-string. Local sessions never have
                          // server-side attachments so we skip the lookup.
                          if (session.is_local) return null;
                          const n = attachmentCounts[String(session.id)] || 0;
                          if (n <= 0) return null;
                          return (
                            <span
                              title={`${n} attachment${n === 1 ? '' : 's'}`}
                              className="inline-flex items-center gap-1 rounded-full border border-blue-500/40 bg-blue-500/10 px-1.5 py-0.5 text-[10px] font-medium text-blue-200"
                            >
                              <Paperclip className="w-3 h-3" />
                              {n}
                            </span>
                          );
                        })()}
                      </h3>
                      {session.title && session.name !== session.title && (
                        <p className="text-gray-500 text-xs mt-0.5">Original: {session.name}</p>
                      )}
                      {session.description && (
                        <p className="text-gray-400 text-sm mt-1 line-clamp-2">{session.description}</p>
                      )}
                      {session.summary_preview ? (
                        <p
                          className="text-gray-500 text-xs mt-1.5 line-clamp-3 leading-snug"
                          title={session.summary_preview}
                        >
                          {session.summary_preview}
                        </p>
                      ) : session.is_local ? (
                        <p className="text-gray-600 text-xs mt-1.5 italic">
                          Live transcript only — no summary on this device
                        </p>
                      ) : null}
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      {!session.is_local && session.organization_name && (
                        <span
                          title={`Organization: ${session.organization_name}`}
                          className="rounded-full border border-gray-700 bg-zinc-800 px-2 py-1 text-xs text-gray-300"
                        >
                          {session.organization_name}
                        </span>
                      )}
                      {session.recorded_by && (
                        <span
                          title={`Recorded on ${session.recorded_by}'s account`}
                          className="rounded-full border border-gray-700 bg-zinc-800 px-2 py-1 text-xs text-gray-400"
                        >
                          rec: {session.recorded_by}
                        </span>
                      )}
                      <span className={`px-2 py-1 rounded-full text-xs border ${getStatusColor(session.status)}`}>
                        {getStatusLabel(session.status)}
                      </span>
                      <ReprocessingBadge session={session} className="px-2 py-1" />
                      {session.summary && (
                        <span className="px-2 py-1 bg-purple-500/20 text-purple-300 text-xs rounded border border-purple-500/30">
                          AI Summary
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Meta */}
                  <div className="space-y-2 text-sm text-gray-400">
                    <div className="flex items-center gap-2">
                      <Calendar className="w-4 h-4" />
                      {new Date(session.created_at).toLocaleDateString()}
                    </div>
                    {session.duration && (
                      <div className="flex items-center gap-2">
                        <Clock className="w-4 h-4" />
                        {formatDuration(session.duration)}
                      </div>
                    )}
                    {session.participants && session.participants.length > 0 && (
                      <div className="flex items-center gap-2">
                        <Users className="w-4 h-4" />
                        <span className="truncate">
                          {session.participants.slice(0, 3).map((p) => p.name).join(', ')}
                          {session.participants.length > 3 && ` +${session.participants.length - 3} more`}
                        </span>
                      </div>
                    )}
                    {/* Named speakers (diarized voices matched to people). Names
                        only — the avatar circles were dropped (mostly initial-letter
                        placeholders with no photo = visual noise + render cost). */}
                    {session.speakers && session.speakers.length > 0 && (
                      <div className="flex items-center gap-2">
                        <Mic className="w-4 h-4 flex-shrink-0" />
                        <span className="truncate">
                          {session.speakers.slice(0, 3).map((s) => s.name).join(', ')}
                          {session.speakers.length > 3 && ` +${session.speakers.length - 3} more`}
                        </span>
                      </div>
                    )}
                  </div>

                  {/* AI Summary Preview */}
                  {(() => {
                    // Match SessionDetails' fallback chain so new-style summaries
                    // (final_summary.executive) render on the card too, not just the
                    // legacy summary.analysis nest.
                    const exec = session.final_summary?.executive
                      || session.summary?.executive
                      || session.summary?.analysis?.executive;
                    const actions = session.final_summary?.actions
                      || session.summary?.actions
                      || session.summary?.analysis?.actions;
                    if (!exec) return null;
                    return (
                      <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700">
                        <div className="flex items-center gap-2 mb-2">
                          <div className="w-2 h-2 bg-purple-400 rounded-full"></div>
                          <span className="text-xs text-purple-300 font-medium">AI Summary</span>
                        </div>
                        <p className="text-gray-300 text-sm line-clamp-2">{exec}</p>
                        {actions && actions.length > 0 && (
                          <div className="mt-2 text-xs text-gray-400">
                            {actions.length} action item{actions.length !== 1 ? 's' : ''}
                          </div>
                        )}
                      </div>
                    );
                  })()}

                  {/* Tags */}
                  {session.tags && session.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {[...session.tags].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())).slice(0, 4).map((tag) => (
                        <TagChip key={tag} tag={tag} />
                      ))}
                      {session.tags.length > 4 && (
                        <span className="px-2 py-0.5 text-xs rounded-full border border-gray-700 bg-gray-800 text-gray-400">
                          +{session.tags.length - 4}
                        </span>
                      )}
                    </div>
                  )}

                  {/* Actions */}
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => navigate(`/sessions/${session.id}`)}
                      className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded transition-colors"
                    >
                      <Eye className="w-4 h-4" />
                      View Details
                    </button>

                    {session.has_transcript && (
                      <button
                        onClick={() => downloadTranscript(session)}
                        className="p-2 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded transition-colors"
                        title="Download Transcript"
                      >
                        <FileText className="w-4 h-4" />
                      </button>
                    )}

                    {session.summary && (
                      <button
                        onClick={() => downloadSummary(session)}
                        className="p-2 bg-green-700 hover:bg-green-600 text-green-300 rounded transition-colors"
                        title="Download Summary"
                      >
                        <FileText className="w-4 h-4" />
                      </button>
                    )}

                    {session.has_audio && (
                      <button
                        onClick={() => downloadAudio(session)}
                        className="p-2 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded transition-colors"
                        title="Download Audio"
                      >
                        <Download className="w-4 h-4" />
                      </button>
                    )}

                    <button
                      onClick={() => deleteSession(session.id)}
                      className="p-2 bg-red-600/20 hover:bg-red-600/30 text-red-400 rounded transition-colors"
                      title="Delete Session"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
              </React.Fragment>
            ))}
          </div>
          ) : (
          <SessionsTable
              sessions={displayedSessions}
              sortBy={sortBy}
              sortOrder={sortOrder}
              onHeaderSort={handleHeaderSort}
              statusFilter={tableStatusFilter}
              speakerFilter={tableSpeakerFilter}
              onStatusFilterChange={setTableStatusFilter}
              onSpeakerFilterChange={setTableSpeakerFilter}
              openColumnFilter={openColumnFilter}
              onOpenColumnFilter={setOpenColumnFilter}
              onOpen={(id) => navigate(`/sessions/${id}`)}
              onDelete={deleteSession}
              selectedSessionIds={selectedSessionIds}
              onToggleSessionSelection={toggleSessionSelection}
              onToggleSelectVisible={toggleSelectVisible}
              formatDuration={formatDuration}
              getStatusColor={getStatusColor}
              getStatusLabel={getStatusLabel}
              dateBasis={dateBasis}
            />
          )
        )}

        {nextCursor && !searchQuery && (
          <div className="my-6 flex justify-center">
            <button
              type="button"
              disabled={loadingMore}
              onClick={() => fetchSessions({ showSpinner: false, cursor: nextCursor, append: true })}
              className="rounded-lg border border-gray-700 bg-gray-900 px-5 py-2 text-sm text-gray-200 hover:border-purple-500 hover:text-white disabled:opacity-50"
            >
              {loadingMore ? 'Loading…' : 'Load more sessions'}
            </button>
          </div>
        )}

        {/* Bulk-action toolbar — visible whenever >= 1 session is selected.
            Built around a BulkAction[] list so more bulk operations can be
            added later; Delete is the shipped action. */}
        <BulkActionsToolbar
          selectedCount={selectedSessionIds.length}
          onClear={clearSessionSelection}
          actions={[
            {
              key: 'export',
              label: bulkExporting
                ? 'Exporting…'
                : `Export ${selectedSessionIds.length} selected`,
              icon: <Download className="h-4 w-4" />,
              onClick: () => { void bulkExportSelected(); },
              disabled: bulkExporting || bulkDeleting || selectedSessionIds.length === 0,
              busy: bulkExporting,
            },
            {
              key: 'delete',
              label: bulkDeleting
                ? 'Deleting…'
                : `Delete ${selectedSessionIds.length} selected`,
              icon: <Trash2 className="h-4 w-4" />,
              variant: 'danger',
              onClick: () => { void bulkDeleteSelected(); },
              disabled: bulkDeleting || bulkExporting || selectedSessionIds.length === 0,
              busy: bulkDeleting,
            },
          ]}
        />
      </div>

{pendingFiles.length > 0 && (
  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
    <div className="w-full max-w-2xl rounded-lg bg-gray-900 border border-gray-700 shadow-2xl">
      <div className="border-b border-gray-800 px-6 py-4">
        <h3 className="text-lg font-semibold text-white">Confirm upload</h3>
        <p className="mt-1 text-sm text-gray-400">
          {pendingFiles.length} file{pendingFiles.length === 1 ? '' : 's'} ready. Pick options below, then Start.
        </p>
      </div>
      <div className="max-h-[60vh] overflow-y-auto px-6 py-4 space-y-4">
        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">Files</p>
          <ul className="space-y-1">
            {pendingFiles.map((f, idx) => (
              <li key={idx} className="flex items-center justify-between text-sm text-gray-200 bg-gray-800/60 rounded px-3 py-2">
                <span className="truncate pr-2">{f.name}</span>
                <span className="text-xs text-gray-500">{(f.size / 1024 / 1024).toFixed(1)} MB</span>
              </li>
            ))}
          </ul>
        </div>
        <TranscriptionOptionsPanel value={uploadOptions} onChange={setUploadOptions} defaultOpen />
      </div>
      <div className="flex items-center justify-end gap-3 border-t border-gray-800 px-6 py-3">
        <button
          onClick={cancelUpload}
          className="px-4 py-2 text-sm text-gray-300 hover:text-white"
        >
          Cancel
        </button>
        <button
          onClick={confirmUpload}
          className="px-4 py-2 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700"
        >
          Start processing
        </button>
      </div>
    </div>
  </div>
)}

    </div>
    </>
  );
};
