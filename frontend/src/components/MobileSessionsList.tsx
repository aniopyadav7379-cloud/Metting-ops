import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Search,
  Calendar,
  Clock,
  Users,
  Mic,
  FolderOpen,
  Plus,
  Loader2,
  RefreshCw,
  X,
  Briefcase,
  AlertTriangle,
} from 'lucide-react';
import { TagChip } from './TagChip';

interface Participant {
  id?: string;
  name: string;
  email?: string | null;
  role?: string | null;
}

interface MobileSession {
  id: string;
  name: string;
  title?: string;
  description?: string;
  created_at: string;
  duration?: number;
  status: string;
  participants?: Participant[];
  tags?: string[];
  transcription?: { segments?: Array<{ speaker?: string }> } | null;
  summary_preview?: string;
  speaker_count?: number;
  speakers?: Array<{ name: string; photo_url?: string | null; raw_label?: string | null }>;
  project_app?: string | null;
  project_slug?: string | null;
  /** Processing metadata. `reprocess_status` tracks the server full-audio
   *  pipeline when an already-completed session is re-reprocessed: it flips
   *  to queued/in_progress while the top-level `status` stays 'completed',
   *  so the row needs this to show any indicator. null/undefined for
   *  privacy-mode or never-uploaded sessions. */
  metadata?: {
    reprocess_status?: 'queued' | 'in_progress' | 'complete' | 'failed' | 'skipped' | null;
    [key: string]: unknown;
  };
}

interface Props {
  sessions: MobileSession[];
  loading: boolean;
  refreshing: boolean;
  searchQuery: string;
  onSearchChange: (value: string) => void;
  folderSummaries: Array<{ tag: string; count: number }>;
  totalSessions: number;
  untaggedSessions: number;
  selectedFolder: string;
  onSelectFolder: (folder: string) => void;
  onNewFolder: () => void;
  orgTags: string[];
  selectedTags: string[];
  onToggleTag: (tag: string) => void;
  onClearTags: () => void;
}

function deriveSpeakerCount(session: MobileSession): number {
  if (typeof session.speaker_count === 'number' && session.speaker_count > 0) {
    return session.speaker_count;
  }
  const segs = session.transcription?.segments;
  if (!Array.isArray(segs) || segs.length === 0) return 0;
  const set = new Set<string>();
  for (const s of segs) {
    if (s && s.speaker) set.add(s.speaker);
  }
  return set.size;
}

function formatDuration(seconds?: number): string {
  if (!seconds) return '--';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatRelativeDate(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.floor(diffH / 24);
  if (diffD < 7) return `${diffD}d ago`;
  return d.toLocaleDateString();
}

function statusPill(status: string): { label: string; cls: string } {
  switch (status) {
    case 'active':
    case 'recording':
      return { label: 'Recording', cls: 'bg-red-500/20 text-red-300 border-red-500/40' };
    case 'completed':
      return { label: 'Done', cls: 'bg-green-500/15 text-green-300 border-green-500/30' };
    case 'processing':
      return { label: 'Processing', cls: 'bg-yellow-500/15 text-yellow-300 border-yellow-500/30' };
    case 'failed':
      return { label: 'Failed', cls: 'bg-red-500/15 text-red-300 border-red-500/30' };
    default:
      return { label: status.charAt(0).toUpperCase() + status.slice(1), cls: 'bg-gray-700/40 text-gray-300 border-gray-600/40' };
  }
}

/**
 * Read-only "Reprocessing…" pill for list cards — mobile parity with the
 * desktop pages/Sessions.tsx ReprocessingBadge. When an already-completed
 * session is re-reprocessed, the server full-audio pipeline flips
 * `metadata.reprocess_status` to queued/in_progress while the top-level
 * `status` stays 'completed' — so the status pill alone shows no change.
 * This surfaces that in-flight state. Display only; renders nothing otherwise.
 */
function ReprocessingBadge({ session }: { session: MobileSession }) {
  const rs = session.metadata?.reprocess_status;
  if (rs !== 'queued' && rs !== 'in_progress') return null;
  return (
    <span
      title="Server is reprocessing this session from full audio"
      className="shrink-0 inline-flex items-center gap-1 rounded-full border border-blue-500/40 bg-blue-500/15 text-blue-300 text-[10px] px-2 py-0.5"
    >
      <span className="h-2 w-2 rounded-full border border-blue-300 border-t-transparent animate-spin" />
      Reprocessing…
    </span>
  );
}

const MobileSessionsList: React.FC<Props> = ({
  sessions,
  loading,
  refreshing,
  searchQuery,
  onSearchChange,
  folderSummaries,
  totalSessions,
  untaggedSessions,
  selectedFolder,
  onSelectFolder,
  onNewFolder,
  orgTags,
  selectedTags,
  onToggleTag,
  onClearTags,
}) => {
  const navigate = useNavigate();
  const tagStripRef = useRef<HTMLDivElement | null>(null);

  const tagsOrdered = useMemo(() => {
    // Selected tags first, then the rest. Stable visual anchor while filtering.
    const selSet = new Set(selectedTags);
    const sel = orgTags.filter((t) => selSet.has(t));
    const rest = orgTags.filter((t) => !selSet.has(t));
    return [...sel, ...rest];
  }, [orgTags, selectedTags]);

  const handleCardTap = useCallback(
    (id: string) => {
      navigate(`/sessions/${id}`);
    },
    [navigate],
  );

  // When all tags get cleared, scroll the strip back to the left so the user
  // doesn't get stranded mid-scroll with no anchor.
  useEffect(() => {
    if (selectedTags.length === 0 && tagStripRef.current) {
      tagStripRef.current.scrollLeft = 0;
    }
  }, [selectedTags.length]);

  return (
    <div
      className="md:hidden flex flex-col bg-gray-950 text-white min-h-screen"
      style={{
        paddingLeft: 'env(safe-area-inset-left)',
        paddingRight: 'env(safe-area-inset-right)',
      }}
    >
      {/* Sticky top: search + tag strip */}
      <div
        className="sticky top-0 z-20 bg-gray-950/95 backdrop-blur border-b border-gray-800"
        style={{ paddingTop: 'env(safe-area-inset-top)' }}
      >
        <div className="px-4 pt-3 pb-2">
          <div className="flex items-center gap-2">
            <div className="flex-1 relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="search"
                inputMode="search"
                enterKeyHint="search"
                value={searchQuery}
                onChange={(e) => onSearchChange(e.target.value)}
                placeholder="Search sessions"
                aria-label="Search sessions"
                className="w-full pl-10 pr-9 py-2.5 bg-gray-900 border border-gray-800 rounded-xl text-base text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => onSearchChange('')}
                  aria-label="Clear search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-500 active:text-white"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
            {refreshing && (
              <div
                className="flex items-center text-purple-300"
                title="Refreshing"
                aria-label="Refreshing"
              >
                <RefreshCw className="w-4 h-4 animate-spin" />
              </div>
            )}
          </div>
        </div>

        {(folderSummaries.length > 0 || totalSessions > 0 || untaggedSessions > 0) && (
          <div className="px-4 pb-2">
            <div className="flex items-center justify-between gap-2 mb-2">
              <div>
                <p className="text-[10px] uppercase tracking-[0.2em] text-gray-500">Folders</p>
                <p className="text-xs text-gray-400">Tags behave like folders.</p>
              </div>
              <button
                type="button"
                onClick={onNewFolder}
                className="shrink-0 inline-flex items-center gap-1 rounded-full border border-purple-500/40 bg-purple-500/10 px-2.5 py-1 text-xs text-purple-200 active:bg-purple-500/20"
              >
                <Plus className="w-3 h-3" />
                New folder
              </button>
            </div>
            <div className="flex items-center gap-2 overflow-x-auto pb-1 -mx-1 px-1 scrollbar-none" style={{ scrollbarWidth: 'none' }}>
              <button
                type="button"
                onClick={() => onSelectFolder('all')}
                className={`shrink-0 inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs ${
                  selectedFolder === 'all'
                    ? 'border-purple-500 bg-purple-600 text-white'
                    : 'border-gray-700 bg-gray-900 text-gray-300 active:bg-gray-800'
                }`}
              >
                All
                <span className="text-[10px] opacity-80">{totalSessions}</span>
              </button>
              <button
                type="button"
                onClick={() => onSelectFolder('__ungrouped__')}
                className={`shrink-0 inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs ${
                  selectedFolder === '__ungrouped__'
                    ? 'border-purple-500 bg-purple-600 text-white'
                    : 'border-gray-700 bg-gray-900 text-gray-300 active:bg-gray-800'
                }`}
              >
                Ungrouped
                <span className="text-[10px] opacity-80">{untaggedSessions}</span>
              </button>
              {folderSummaries.map(({ tag, count }) => {
                const selected = selectedFolder === tag;
                return (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => onSelectFolder(tag)}
                    className={`shrink-0 inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs ${
                      selected
                        ? 'border-purple-500 bg-purple-600 text-white'
                        : 'border-gray-700 bg-gray-900 text-gray-300 active:bg-gray-800'
                    }`}
                  >
                    <span className="max-w-[10rem] truncate">{tag}</span>
                    <span className="text-[10px] opacity-80">{count}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {orgTags.length > 0 && (
          <div className="px-4 pb-3">
            <div
              ref={tagStripRef}
              className="flex items-center gap-2 overflow-x-auto pb-1 -mx-1 px-1 scrollbar-none"
              style={{ scrollbarWidth: 'none' }}
            >
              {selectedTags.length > 0 && (
                <button
                  type="button"
                  onClick={onClearTags}
                  className="shrink-0 inline-flex items-center gap-1 rounded-full border border-gray-700 bg-gray-900 text-gray-300 text-xs px-2.5 py-1 active:bg-gray-800"
                  aria-label="Clear tag filters"
                >
                  <X className="w-3 h-3" /> Clear ({selectedTags.length})
                </button>
              )}
              {tagsOrdered.map((tag) => {
                const selected = selectedTags.includes(tag);
                return (
                  <div key={tag} className="shrink-0">
                    <TagChip
                      tag={tag}
                      variant="filter"
                      selected={selected}
                      onClick={() => onToggleTag(tag)}
                    />
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* List body */}
      <div className="flex-1 px-3 pt-2 pb-10">
        {loading && sessions.length === 0 ? (
          <div className="flex items-center justify-center py-16">
            <div className="text-center text-gray-400">
              <Loader2 className="w-7 h-7 animate-spin mx-auto mb-3 text-purple-400" />
              <p className="text-sm">Loading sessions…</p>
            </div>
          </div>
        ) : sessions.length === 0 ? (
          <div className="text-center py-16 px-6">
            <FolderOpen className="w-14 h-14 text-gray-700 mx-auto mb-4" />
            <h3 className="text-lg font-semibold text-gray-200 mb-1">No sessions yet</h3>
            <p className="text-sm text-gray-500 mb-6">
              {searchQuery || selectedTags.length > 0
                ? 'Try clearing your search or tag filters.'
                : 'Start a recording from your phone and it will appear here.'}
            </p>
            {!searchQuery && selectedTags.length === 0 && (
              <button
                type="button"
                onClick={() => navigate('/record')}
                className="inline-flex items-center gap-2 px-5 py-3 rounded-xl bg-gradient-to-r from-fuchsia-600 to-indigo-600 text-white text-sm font-semibold active:from-fuchsia-500 active:to-indigo-500"
              >
                <Mic className="w-5 h-5" />
                Record on this phone
              </button>
            )}
          </div>
        ) : (
          <ul className="space-y-2.5">
            {sessions.map((session) => {
              const speakerCount = deriveSpeakerCount(session);
              const participants = session.participants || [];
              const pill = statusPill(session.status);
              const tags = session.tags || [];
              const visibleTags = tags.slice(0, 3);
              const hiddenTagCount = tags.length - visibleTags.length;
              const titleText = session.title || session.name || 'Untitled';
              return (
                <li key={session.id}>
                  <button
                    type="button"
                    onClick={() => handleCardTap(session.id)}
                    className="w-full text-left rounded-xl bg-gray-900 border border-gray-800 active:bg-gray-900/70 active:border-gray-700 transition-colors px-4 py-3 min-h-[64px] flex flex-col gap-2"
                  >
                    <div className="flex items-start gap-2">
                      <h3 className="flex-1 text-base font-semibold text-white leading-snug break-words line-clamp-2">
                        {titleText}
                      </h3>
                      <span
                        className={`shrink-0 text-[10px] font-medium uppercase tracking-wide px-2 py-0.5 rounded-full border ${pill.cls}`}
                      >
                        {pill.label}
                      </span>
                      <ReprocessingBadge session={session} />
                    </div>

                    {session.project_app && session.project_slug && (
                      <span
                        className={`inline-flex w-fit items-center gap-1 px-1.5 py-0.5 rounded-full border text-[10px] ${
                          session.project_app === 'crisis-ops'
                            ? 'border-red-800/60 bg-red-900/20 text-red-300'
                            : 'border-purple-800/60 bg-purple-900/20 text-purple-300'
                        }`}
                      >
                        {session.project_app === 'crisis-ops'
                          ? <AlertTriangle className="w-2.5 h-2.5" />
                          : <Briefcase className="w-2.5 h-2.5" />}
                        {session.project_slug}
                      </span>
                    )}

                    {session.summary_preview && (
                      <p className="text-xs text-gray-400 leading-relaxed line-clamp-2">
                        {session.summary_preview}
                      </p>
                    )}

                    {/* Named speakers (names only — avatar circles dropped). */}
                    {session.speakers && session.speakers.length > 0 && (
                      <div className="flex items-center gap-1.5 text-xs text-gray-400">
                        <Mic className="w-3.5 h-3.5 flex-shrink-0" />
                        <span className="truncate">
                          {session.speakers.slice(0, 2).map((s) => s.name).join(', ')}
                          {session.speakers.length > 2 && ` +${session.speakers.length - 2}`}
                        </span>
                      </div>
                    )}

                    <div className="flex items-center gap-3 text-xs text-gray-400 flex-wrap">
                      <span className="inline-flex items-center gap-1">
                        <Calendar className="w-3.5 h-3.5" />
                        {formatRelativeDate(session.created_at)}
                      </span>
                      <span className="inline-flex items-center gap-1">
                        <Clock className="w-3.5 h-3.5" />
                        {formatDuration(session.duration)}
                      </span>
                      {speakerCount > 0 && (
                        <span className="inline-flex items-center gap-1">
                          <Mic className="w-3.5 h-3.5" />
                          {speakerCount} {speakerCount === 1 ? 'speaker' : 'speakers'}
                        </span>
                      )}
                      {participants.length > 0 && (
                        <span className="inline-flex items-center gap-1">
                          <Users className="w-3.5 h-3.5" />
                          {participants.length === 1
                            ? participants[0].name
                            : `${participants[0].name} +${participants.length - 1}`}
                        </span>
                      )}
                    </div>

                    {visibleTags.length > 0 && (
                      <div className="flex items-center gap-1 flex-wrap">
                        {visibleTags.map((tag) => (
                          <TagChip key={tag} tag={tag} />
                        ))}
                        {hiddenTagCount > 0 && (
                          <span className="px-2 py-0.5 text-xs rounded-full border border-gray-700 bg-gray-800 text-gray-400">
                            +{hiddenTagCount} more
                          </span>
                        )}
                      </div>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
};

export default MobileSessionsList;
