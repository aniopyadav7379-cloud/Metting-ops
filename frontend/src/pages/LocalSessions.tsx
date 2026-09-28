import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  Calendar,
  Check,
  ChevronRight,
  Clock,
  Download,
  FileJson,
  FileText,
  Hash,
  Loader2,
  Lock,
  Pencil,
  Play,
  RefreshCw,
  Trash2,
  Users,
  X,
} from 'lucide-react';
import {
  deleteLocalSession,
  exportLocalSession,
  exportLocalSessionJson,
  getLocalSession,
  listLocalSessions,
  slugifyTitle,
  updateLocalSessionMeta,
  type LocalParakeetPassStatus,
  type LocalSession,
} from '../services/localSessionStore';
import { localAudioStore } from '../services/localAudioStore';
import { showConfirm } from '../utils/notifications';

/**
 * Local Sessions surface. Lists privacy-mode sessions persisted in
 * IndexedDB and renders a detail view with audio playback, inline
 * title/tag edit, and Markdown / JSON export.
 *
 * The same component handles list + detail based on the `:id` route
 * param — keeps imports + state contained to one file per Aaron's
 * "don't sprawl pages unless we need to" preference.
 *
 * Everything here reads/writes the browser's IndexedDB only. No
 * network requests for content. The OIDC route guard is applied by
 * AppRouterSimplified.tsx so unauthenticated users can't even land
 * on the page; the data itself isn't auth-controlled because it
 * never left the device.
 */
export default function LocalSessions() {
  const params = useParams<{ id?: string }>();
  return params.id ? <LocalSessionDetail id={params.id} /> : <LocalSessionsList />;
}

// =============================================================
// List view
// =============================================================

function LocalSessionsList() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<LocalSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const items = await listLocalSessions();
      setSessions(items);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      setSessions([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDelete = useCallback(
    async (id: string) => {
      const ok = await showConfirm(
        'Delete this local session? The transcript, summary, and audio for this meeting will be permanently removed from this browser. Nothing on a server is affected.',
        { title: 'Delete local session', confirmLabel: 'Delete' },
      );
      if (!ok) return;
      await deleteLocalSession(id);
      // Also wipe the audio blob in localAudioStore (best-effort).
      try {
        if (localAudioStore.isAvailable()) {
          await localAudioStore.wipeSession(id);
        }
      } catch {
        /* non-fatal */
      }
      await refresh();
    },
    [refresh],
  );

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <div className="p-6">
        <div className="flex items-start justify-between mb-6 gap-4">
          <div>
            <div className="flex items-center gap-2 text-purple-300 text-xs uppercase tracking-wider">
              <Lock className="w-3.5 h-3.5" />
              <span>Local-only</span>
            </div>
            <h1 className="text-3xl font-bold text-white mt-1">Local Sessions</h1>
            <p className="text-gray-400 mt-1">
              Privacy-mode recordings persisted in this browser. The audio,
              transcript, and summary never left your device.
            </p>
          </div>
          <button
            onClick={() => void refresh()}
            className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg transition-colors"
            title="Reload from IndexedDB"
          >
            <RefreshCw className="w-4 h-4" />
            Refresh
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-red-700/40 bg-red-900/30 px-4 py-3 text-sm text-red-200">
            {error}
          </div>
        )}

        {sessions === null ? (
          <div className="flex items-center gap-2 text-gray-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            Loading local sessions…
          </div>
        ) : sessions.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="grid gap-3">
            {sessions.map((session) => (
              <LocalSessionCard
                key={session.id}
                session={session}
                onOpen={() => navigate(`/local-sessions/${session.id}`)}
                onDelete={() => void handleDelete(session.id)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900/40 p-10 text-center">
      <Lock className="w-10 h-10 mx-auto mb-3 text-gray-600" />
      <div className="text-gray-300 font-medium">No local sessions yet</div>
      <p className="text-gray-500 text-sm mt-2 max-w-md mx-auto">
        Local sessions appear here when you record in Privacy mode. Toggle the
        lock icon in the always-on recorder before you start a meeting and the
        audio, transcript, and summary will stay on this device.
      </p>
    </div>
  );
}

function LocalSessionCard({
  session,
  onOpen,
  onDelete,
}: {
  session: LocalSession;
  onOpen: () => void;
  onDelete: () => void;
}) {
  const summaryPreview = useMemo(() => {
    const candidate =
      (session.finalSummary || '').trim()
      || session.slices.map((s) => s.text).join(' ').trim()
      || (session.transcriptFull || '').trim()
      || session.transcript.map((c) => c.text).join(' ').trim();
    return candidate.slice(0, 280);
  }, [session]);

  return (
    <div className="group flex items-center gap-4 rounded-lg border border-gray-800 bg-gray-900/60 px-4 py-3 hover:border-purple-500/40 hover:bg-gray-900 transition-colors">
      <button
        type="button"
        onClick={onOpen}
        className="flex-1 text-left min-w-0"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <LocalOnlyBadge />
          <PassStatusBadge status={session.parakeetPassStatus} />
          <h3 className="text-white font-medium truncate">{session.title}</h3>
        </div>
        <div className="mt-1 flex items-center gap-4 text-xs text-gray-500">
          <span className="flex items-center gap-1">
            <Calendar className="w-3.5 h-3.5" />
            {formatDateTime(session.startedAt)}
          </span>
          <span className="flex items-center gap-1">
            <Clock className="w-3.5 h-3.5" />
            {formatDuration(session.durationSeconds)}
          </span>
          <span className="flex items-center gap-1">
            <Hash className="w-3.5 h-3.5" />
            {session.wordCount.toLocaleString()} words
          </span>
          {session.tags.length > 0 && (
            <span className="flex items-center gap-1">
              <span className="text-gray-500">tags:</span>
              <span className="text-gray-400 truncate">
                {session.tags.slice(0, 4).join(', ')}
                {session.tags.length > 4 ? '…' : ''}
              </span>
            </span>
          )}
        </div>
        {summaryPreview && (
          <p className="mt-2 text-sm text-gray-300 line-clamp-2">{summaryPreview}</p>
        )}
      </button>
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={onDelete}
          className="p-2 rounded-md text-red-400 hover:text-red-300 hover:bg-red-600/20 transition-colors"
          title="Delete local session"
        >
          <Trash2 className="w-4 h-4" />
        </button>
        <ChevronRight className="w-5 h-5 text-gray-600 group-hover:text-purple-300 transition-colors" />
      </div>
    </div>
  );
}

function LocalOnlyBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-purple-500/40 bg-purple-500/15 px-2 py-0.5 text-[10px] font-semibold tracking-wide uppercase text-purple-200">
      <Lock className="w-3 h-3" />
      Local-only
    </span>
  );
}

function PassStatusBadge({ status }: { status: LocalParakeetPassStatus | undefined }) {
  if (!status || status === 'not_run') return null;
  if (status === 'complete') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-200">
        <Check className="w-3 h-3" />
        Full-audio pass
      </span>
    );
  }
  if (status === 'running') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 text-[10px] font-medium text-sky-200">
        <Loader2 className="w-3 h-3 animate-spin" />
        Full-audio running
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-200">
      Live-quality only
    </span>
  );
}

// =============================================================
// Detail view
// =============================================================

interface AudioState {
  loading: boolean;
  url: string | null;
  bytes: number | null;
  mime: string | null;
  error: string | null;
}

function LocalSessionDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const [session, setSession] = useState<LocalSession | null | 'missing'>(null);
  const [error, setError] = useState<string | null>(null);
  const [audio, setAudio] = useState<AudioState>({
    loading: false,
    url: null,
    bytes: null,
    mime: null,
    error: null,
  });
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [tagDraft, setTagDraft] = useState('');
  const [transcriptExpanded, setTranscriptExpanded] = useState(false);
  const [includeAudioInJson, setIncludeAudioInJson] = useState(false);
  const [jsonExporting, setJsonExporting] = useState(false);
  const audioUrlRef = useRef<string | null>(null);

  // Single read on mount + when id changes. Mutations re-read.
  const refresh = useCallback(async () => {
    try {
      const next = await getLocalSession(id);
      setSession(next ?? 'missing');
      if (next) {
        setTitleDraft(next.title);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Assemble audio blob lazily on first request. Skip if we already
  // have a URL or if there's no underlying audio (very short session).
  const loadAudio = useCallback(async () => {
    if (audio.loading || audio.url) return;
    if (!localAudioStore.isAvailable()) {
      setAudio((prev) => ({ ...prev, error: 'IndexedDB unavailable in this browser.' }));
      return;
    }
    setAudio((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const assembled = await localAudioStore.getAssembledBlob(id);
      const url = URL.createObjectURL(assembled.blob);
      audioUrlRef.current = url;
      setAudio({
        loading: false,
        url,
        bytes: assembled.bytes,
        mime: assembled.blob.type || null,
        error: null,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setAudio({
        loading: false,
        url: null,
        bytes: null,
        mime: null,
        error: message,
      });
    }
  }, [id, audio.loading, audio.url]);

  useEffect(() => {
    return () => {
      if (audioUrlRef.current) {
        URL.revokeObjectURL(audioUrlRef.current);
        audioUrlRef.current = null;
      }
    };
  }, []);

  const handleSaveTitle = useCallback(async () => {
    const trimmed = titleDraft.trim();
    if (!trimmed) {
      setEditingTitle(false);
      return;
    }
    await updateLocalSessionMeta(id, { title: trimmed });
    setEditingTitle(false);
    await refresh();
  }, [id, refresh, titleDraft]);

  const handleAddTag = useCallback(async () => {
    if (!session || session === 'missing') return;
    const value = tagDraft.trim();
    if (!value) return;
    if (session.tags.includes(value)) {
      setTagDraft('');
      return;
    }
    await updateLocalSessionMeta(id, { tags: [...session.tags, value] });
    setTagDraft('');
    await refresh();
  }, [id, refresh, session, tagDraft]);

  const handleRemoveTag = useCallback(
    async (tag: string) => {
      if (!session || session === 'missing') return;
      await updateLocalSessionMeta(id, {
        tags: session.tags.filter((t) => t !== tag),
      });
      await refresh();
    },
    [id, refresh, session],
  );

  const handleDelete = useCallback(async () => {
    const ok = await showConfirm(
      'Delete this local session? The audio, transcript, and summary will be permanently removed from this browser.',
      { title: 'Delete local session', confirmLabel: 'Delete' },
    );
    if (!ok) return;
    await deleteLocalSession(id);
    try {
      if (localAudioStore.isAvailable()) {
        await localAudioStore.wipeSession(id);
      }
    } catch {
      /* non-fatal */
    }
    navigate('/local-sessions');
  }, [id, navigate]);

  const handleExportMarkdown = useCallback(async () => {
    if (!session || session === 'missing') return;
    const blob = await exportLocalSession(id);
    if (!blob) return;
    downloadBlob(
      blob,
      `meeting-${formatFilenameDate(session.startedAt)}-${slugifyTitle(session.title)}.md`,
    );
  }, [id, session]);

  const handleExportJson = useCallback(async () => {
    if (!session || session === 'missing') return;
    setJsonExporting(true);
    try {
      let audioBase64: string | null = null;
      if (includeAudioInJson) {
        {
          const sizeMB = (audio.bytes ?? session.audioBytes ?? 0) / 1_000_000;
          const ok = await showConfirm(
            `Include audio in the JSON export?\n\nThe assembled audio (~${sizeMB.toFixed(1)}MB) will be embedded as base64, which can balloon the file to ~${(sizeMB * 1.37).toFixed(1)}MB. Skip this if you only need the transcript + summary.`,
            { title: 'Include audio?', confirmLabel: 'Include audio', tone: 'primary' },
          );
          if (!ok) {
            setJsonExporting(false);
            return;
          }
        }
        try {
          if (!localAudioStore.isAvailable()) {
            throw new Error('Local audio store unavailable.');
          }
          const assembled = await localAudioStore.getAssembledBlob(id);
          audioBase64 = await blobToBase64(assembled.blob);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          setError(`Failed to read audio for JSON export: ${message}`);
          setJsonExporting(false);
          return;
        }
      }
      const blob = await exportLocalSessionJson(id, audioBase64);
      if (!blob) {
        setJsonExporting(false);
        return;
      }
      downloadBlob(
        blob,
        `meeting-${formatFilenameDate(session.startedAt)}-${slugifyTitle(session.title)}.json`,
      );
    } finally {
      setJsonExporting(false);
    }
  }, [audio.bytes, id, includeAudioInJson, session]);

  // useMemo must run unconditionally before any early return — keep
  // hooks order stable across renders. We compute against the loaded
  // session when available, otherwise an empty fallback.
  const liveTranscript = useMemo(() => {
    if (!session || session === 'missing') return '';
    return session.transcript
      .map((chunk) => `[${formatTimestamp(chunk.elapsedSeconds)}] ${chunk.text.trim()}`)
      .join('\n');
  }, [session]);

  if (session === null) {
    return (
      <div className="min-h-screen bg-gray-950 text-white p-6">
        <div className="flex items-center gap-2 text-gray-400">
          <Loader2 className="w-4 h-4 animate-spin" />
          Loading…
        </div>
      </div>
    );
  }

  if (session === 'missing') {
    return (
      <div className="min-h-screen bg-gray-950 text-white p-6">
        <button
          type="button"
          onClick={() => navigate('/local-sessions')}
          className="flex items-center gap-2 text-gray-400 hover:text-white mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Local Sessions
        </button>
        <div className="rounded-lg border border-gray-800 bg-gray-900/40 p-6 text-gray-300">
          This local session can't be found in your browser. It may have been
          deleted, or it lives in a different browser profile.
        </div>
      </div>
    );
  }

  const finalText = (session.finalSummary || '').trim();
  const fullTranscript = (session.transcriptFull || '').trim();

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <div className="p-6 max-w-5xl mx-auto">
        <button
          type="button"
          onClick={() => navigate('/local-sessions')}
          className="flex items-center gap-2 text-gray-400 hover:text-white mb-4"
        >
          <ArrowLeft className="w-4 h-4" />
          Back to Local Sessions
        </button>

        {error && (
          <div className="mb-4 rounded-lg border border-red-700/40 bg-red-900/30 px-4 py-3 text-sm text-red-200">
            {error}
          </div>
        )}

        <div className="rounded-lg border border-gray-800 bg-gray-900/60 p-5">
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <LocalOnlyBadge />
            <PassStatusBadge status={session.parakeetPassStatus} />
            {session.parakeetPassStatus === 'failed' && session.parakeetPassError && (
              <span className="text-xs text-amber-200">
                {session.parakeetPassError}
              </span>
            )}
          </div>

          <div className="flex items-start gap-3">
            {editingTitle ? (
              <div className="flex items-center gap-2 flex-1">
                <input
                  value={titleDraft}
                  onChange={(event) => setTitleDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void handleSaveTitle();
                    if (event.key === 'Escape') {
                      setEditingTitle(false);
                      setTitleDraft(session.title);
                    }
                  }}
                  className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-xl focus:outline-none focus:border-purple-500"
                  autoFocus
                />
                <button
                  type="button"
                  onClick={() => void handleSaveTitle()}
                  className="px-3 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded transition-colors"
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditingTitle(false);
                    setTitleDraft(session.title);
                  }}
                  className="px-3 py-2 text-gray-400 hover:text-white"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <>
                <h1 className="text-2xl font-bold text-white flex-1">{session.title}</h1>
                <button
                  type="button"
                  onClick={() => setEditingTitle(true)}
                  className="p-2 rounded-md text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
                  title="Rename"
                >
                  <Pencil className="w-4 h-4" />
                </button>
              </>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-gray-400">
            <span className="flex items-center gap-1">
              <Calendar className="w-3.5 h-3.5" />
              {formatDateTime(session.startedAt)}
            </span>
            <span className="flex items-center gap-1">
              <Clock className="w-3.5 h-3.5" />
              {formatDuration(session.durationSeconds)}
            </span>
            <span className="flex items-center gap-1">
              <Hash className="w-3.5 h-3.5" />
              {session.wordCount.toLocaleString()} words
            </span>
            {session.participants.length > 0 && (
              <span className="flex items-center gap-1">
                <Users className="w-3.5 h-3.5" />
                {session.participants.join(', ')}
              </span>
            )}
          </div>

          {/* Tag editor */}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {session.tags.map((tag) => (
              <span
                key={tag}
                className="inline-flex items-center gap-1 rounded-full border border-gray-700 bg-gray-800 px-2 py-0.5 text-xs text-gray-200"
              >
                {tag}
                <button
                  type="button"
                  onClick={() => void handleRemoveTag(tag)}
                  className="text-gray-500 hover:text-red-400"
                  title="Remove tag"
                >
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
            <div className="flex items-center gap-1">
              <input
                value={tagDraft}
                onChange={(event) => setTagDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void handleAddTag();
                }}
                placeholder="Add tag"
                className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white placeholder-gray-500 focus:outline-none focus:border-purple-500 w-32"
              />
              <button
                type="button"
                onClick={() => void handleAddTag()}
                className="px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-200 rounded"
              >
                Add
              </button>
            </div>
          </div>

          {/* Action buttons */}
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void handleExportMarkdown()}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 rounded transition-colors text-sm"
            >
              <FileText className="w-4 h-4" />
              Export as Markdown
            </button>
            <button
              type="button"
              onClick={() => void handleExportJson()}
              disabled={jsonExporting}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 text-gray-200 rounded transition-colors text-sm"
            >
              {jsonExporting ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <FileJson className="w-4 h-4" />
              )}
              Export as JSON
            </button>
            <label className="flex items-center gap-2 text-xs text-gray-400 pl-1">
              <input
                type="checkbox"
                checked={includeAudioInJson}
                onChange={(event) => setIncludeAudioInJson(event.target.checked)}
                className="accent-purple-500"
              />
              Include audio (base64)
            </label>
            <div className="flex-1" />
            <button
              type="button"
              onClick={() => void handleDelete()}
              className="flex items-center gap-2 px-3 py-2 bg-red-900/30 hover:bg-red-900/50 text-red-300 rounded transition-colors text-sm"
            >
              <Trash2 className="w-4 h-4" />
              Delete
            </button>
          </div>
        </div>

        {/* Summary card */}
        <section className="mt-6 rounded-lg border border-gray-800 bg-gray-900/60 p-5">
          <h2 className="text-lg font-semibold text-white mb-3">Summary</h2>
          {finalText ? (
            <pre className="whitespace-pre-wrap text-sm text-gray-200 font-sans leading-relaxed">
              {finalText}
            </pre>
          ) : (
            <div className="text-sm text-gray-500">
              No summary was generated for this session.
            </div>
          )}
        </section>

        {/* Audio playback */}
        <section className="mt-6 rounded-lg border border-gray-800 bg-gray-900/60 p-5">
          <h2 className="text-lg font-semibold text-white mb-3">Audio</h2>
          {audio.url ? (
            <div>
              <audio controls className="w-full" src={audio.url} />
              <div className="mt-2 text-xs text-gray-500">
                {audio.mime || 'audio'} ·{' '}
                {audio.bytes != null ? formatBytes(audio.bytes) : 'unknown size'}
              </div>
            </div>
          ) : audio.error ? (
            <div className="text-sm text-amber-300">{audio.error}</div>
          ) : (
            <button
              type="button"
              onClick={() => void loadAudio()}
              disabled={audio.loading}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 text-gray-200 rounded transition-colors text-sm"
            >
              {audio.loading ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Play className="w-4 h-4" />
              )}
              Load audio
            </button>
          )}
        </section>

        {/* Full transcript (collapsible) */}
        <section className="mt-6 rounded-lg border border-gray-800 bg-gray-900/60 p-5">
          <button
            type="button"
            onClick={() => setTranscriptExpanded((prev) => !prev)}
            className="flex w-full items-center justify-between text-lg font-semibold text-white"
          >
            <span>
              Transcript
              {fullTranscript ? (
                <span className="ml-2 text-xs font-normal text-emerald-300">
                  full-audio pass
                </span>
              ) : (
                <span className="ml-2 text-xs font-normal text-gray-500">
                  live chunks only
                </span>
              )}
            </span>
            <ChevronRight
              className={`w-5 h-5 text-gray-500 transition-transform ${
                transcriptExpanded ? 'rotate-90' : ''
              }`}
            />
          </button>
          {transcriptExpanded && (
            <div className="mt-3">
              {fullTranscript ? (
                <pre className="whitespace-pre-wrap text-sm text-gray-200 font-sans leading-relaxed">
                  {fullTranscript}
                </pre>
              ) : liveTranscript ? (
                <pre className="whitespace-pre-wrap text-sm text-gray-300 font-mono text-xs leading-relaxed">
                  {liveTranscript}
                </pre>
              ) : (
                <div className="text-sm text-gray-500">No transcript captured.</div>
              )}
              {fullTranscript && liveTranscript && (
                <details className="mt-4">
                  <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-300">
                    Show live (chunked) transcript instead
                  </summary>
                  <pre className="mt-2 whitespace-pre-wrap text-xs text-gray-400 font-mono leading-relaxed">
                    {liveTranscript}
                  </pre>
                </details>
              )}
            </div>
          )}
        </section>

        {session.slices.length > 0 && (
          <section className="mt-6 rounded-lg border border-gray-800 bg-gray-900/60 p-5">
            <h2 className="text-lg font-semibold text-white mb-3">
              Live slice summaries{' '}
              <span className="ml-2 text-xs font-normal text-gray-500">
                {session.slices.length} slice{session.slices.length === 1 ? '' : 's'}
              </span>
            </h2>
            <div className="space-y-4">
              {session.slices.map((slice, idx) => (
                <div key={`${slice.createdAt}-${idx}`} className="border-l-2 border-purple-700 pl-3">
                  <div className="text-xs text-gray-500 mb-1">
                    {formatDateTime(slice.createdAt)} · {slice.model}
                  </div>
                  <pre className="whitespace-pre-wrap text-sm text-gray-200 font-sans leading-relaxed">
                    {slice.text.trim()}
                  </pre>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}

// =============================================================
// Helpers
// =============================================================

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

function formatFilenameDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'session';
  return [
    d.getFullYear(),
    String(d.getMonth() + 1).padStart(2, '0'),
    String(d.getDate()).padStart(2, '0'),
  ].join('');
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatTimestamp(elapsed: number): string {
  if (!Number.isFinite(elapsed) || elapsed < 0) return '00:00';
  const total = Math.floor(elapsed);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function downloadBlob(blob: Blob, filename: string): void {
  if (typeof document === 'undefined') return;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke after a small delay so the browser has time to start the
  // download — revoking synchronously cancels in some Safari builds.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function blobToBase64(blob: Blob): Promise<string> {
  // FileReader.readAsDataURL is the most reliable cross-browser path
  // for large blobs; chunked alternatives that build base64 manually
  // hit stack-overflow limits on multi-MB strings.
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result;
      if (typeof result !== 'string') {
        reject(new Error('FileReader returned non-string result.'));
        return;
      }
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error('FileReader failed.'));
    reader.readAsDataURL(blob);
  });
}
