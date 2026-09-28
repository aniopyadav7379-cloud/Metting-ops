import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronDown,
  ChevronUp,
  Loader2,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { config } from '../../config';
import roomsApi, { type SummarySlice } from '../../services/roomsApi';

interface RoomLiveSummaryProps {
  /** Active session UUID. */
  sessionId: string | null;
  /** Active org slug for the X-MeetingOps-Org header. */
  orgSlug: string | null;
  /** Whether the room is currently recording. */
  recording: boolean;
  /**
   * Current transcript text — provided by the parent so we don't poll
   * twice. Only used for the non-room (browser always-on) client-rolled
   * fallback; room sessions ignore this and rely on the server-rolled
   * slice stack instead.
   */
  transcript: string;
  /**
   * `true` when this session is a conference-room recording (driving the
   * server-rolled slice persistence). When falsy or `null`/`undefined` we
   * keep the legacy client-rolled behaviour so browser always-on sessions
   * aren't affected by this change.
   */
  isRoomSession?: boolean | null;
  /** Word-count delta that triggers a client-rolled auto-summary
   * (non-room sessions only). Default 200. */
  wordTriggerDelta?: number;
  /** Minimum seconds between client-rolled auto-summaries
   * (non-room sessions only). Default 90. */
  minIntervalSec?: number;
  /** How often to poll the server-rolled slice stack (room sessions
   * only). Default 5s — matches the auto-trigger cadence well enough
   * that the UI never feels stale, without hammering the API. */
  serverPollMs?: number;
}

interface ClientSlice {
  id: string;
  text: string;
  createdAt: string;
  wordCount: number;
}

function countWords(text: string): number {
  if (!text) return 0;
  const matches = text.trim().match(/\S+/g);
  return matches ? matches.length : 0;
}

function uuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `slice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  });
}

/**
 * Renders the rolling auto-summary stack for a recording session.
 *
 * Two modes:
 *   1. Room sessions (`isRoomSession === true`) — slices are persisted
 *      server-side in `RecordingSession.processing_metadata.summary_slices`.
 *      The component polls `GET /api/recordings/sessions/{id}/summary-slices`
 *      every `serverPollMs` and renders the server's slice stack. The
 *      "Summarize now" button POSTs to the same endpoint so manual +
 *      auto-triggered slices land in the same store. This is the path
 *      that gives multiple viewers + after-the-fact reviewers the same
 *      slice timeline.
 *
 *   2. Non-room sessions (browser always-on) — slices live in component
 *      state. The component watches transcript word count and POSTs to
 *      `/api/recordings/summarize` when the delta crosses the threshold.
 *      Unchanged from the original implementation.
 *
 * Latest slice is expanded; older slices collapse to a header row.
 */
export default function RoomLiveSummary({
  sessionId,
  orgSlug,
  recording,
  transcript,
  isRoomSession = false,
  wordTriggerDelta = 200,
  minIntervalSec = 90,
  serverPollMs = 5000,
}: RoomLiveSummaryProps) {
  // Server-rolled slices (room sessions).
  const [serverSlices, setServerSlices] = useState<SummarySlice[]>([]);
  // Client-rolled slices (non-room fallback).
  const [clientSlices, setClientSlices] = useState<ClientSlice[]>([]);
  const [summarizing, setSummarizing] = useState(false);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  // Client-mode trigger bookkeeping (unused in room mode).
  const lastWordsAtSummaryRef = useRef(0);
  const lastSummaryTimeRef = useRef(0);

  // Reset state when session changes — slice stack must be per-session.
  useEffect(() => {
    setServerSlices([]);
    setClientSlices([]);
    setExpandedIds(new Set());
    setError(null);
    lastWordsAtSummaryRef.current = 0;
    lastSummaryTimeRef.current = 0;
  }, [sessionId]);

  // === Server-rolled path (room sessions) ==============================

  const refreshServerSlices = useCallback(async () => {
    if (!sessionId || !isRoomSession) return;
    setPolling(true);
    try {
      const data = await roomsApi.getSummarySlices(sessionId, { orgSlug });
      setServerSlices(data.slices);
      setError(null);
    } catch (err) {
      // Don't surface network blips while polling — they self-heal on
      // the next tick. We keep the last good slice list so the UI
      // doesn't flicker.
      // eslint-disable-next-line no-console
      console.debug('[RoomLiveSummary] poll failed', err);
    } finally {
      setPolling(false);
    }
  }, [sessionId, isRoomSession, orgSlug]);

  // Server-side poll loop while the room is recording. We also run one
  // refresh on mount / session change so after-the-fact reviewers see
  // the completed slice list even when not recording.
  useEffect(() => {
    if (!sessionId || !isRoomSession) return;
    void refreshServerSlices();
    if (!recording) return;
    const handle = window.setInterval(() => {
      void refreshServerSlices();
    }, Math.max(1500, serverPollMs));
    return () => window.clearInterval(handle);
  }, [sessionId, isRoomSession, recording, serverPollMs, refreshServerSlices]);

  const runServerSlice = useCallback(async () => {
    if (!sessionId || !isRoomSession) return;
    setSummarizing(true);
    setError(null);
    try {
      const data = await roomsApi.createSummarySlice(sessionId, { orgSlug });
      // Append optimistically; the next poll will reconcile if the
      // server has added auto-triggered slices in the meantime.
      setServerSlices((prev) => {
        if (prev.some((s) => s.id === data.slice.id)) return prev;
        return [...prev, data.slice];
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Summary failed');
    } finally {
      setSummarizing(false);
    }
  }, [sessionId, isRoomSession, orgSlug]);

  // === Client-rolled path (non-room fallback) ==========================
  //
  // Unchanged from the original implementation — keeps browser always-on
  // sessions working exactly as they did before. Room sessions skip all
  // of this and rely on the server poll above.

  const runClientSummary = useCallback(async () => {
    if (!sessionId) return;
    if (!transcript || !transcript.trim()) return;
    setSummarizing(true);
    setError(null);
    try {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
      };
      try {
        const token = localStorage.getItem('access_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
      } catch {
        /* ignore */
      }
      if (orgSlug) headers['X-MeetingOps-Org'] = orgSlug;
      const resp = await fetch(`${config.apiBaseUrl}/api/recordings/summarize`, {
        method: 'POST',
        credentials: 'include',
        headers,
        body: JSON.stringify({
          transcript,
          session_id: sessionId,
        }),
      });
      if (!resp.ok) {
        let detail = `Status ${resp.status}`;
        try {
          const body = await resp.json();
          if (typeof body?.detail === 'string') detail = body.detail;
        } catch {
          /* ignore */
        }
        throw new Error(detail);
      }
      const data = (await resp.json()) as { summary?: string };
      const text = (data?.summary || '').trim();
      if (!text) {
        throw new Error('Empty summary response');
      }
      const wc = countWords(transcript);
      const slice: ClientSlice = {
        id: uuid(),
        text,
        createdAt: new Date().toISOString(),
        wordCount: wc,
      };
      setClientSlices((prev) => [...prev, slice]);
      lastWordsAtSummaryRef.current = wc;
      lastSummaryTimeRef.current = Date.now();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Summary failed');
    } finally {
      setSummarizing(false);
    }
  }, [sessionId, transcript, orgSlug]);

  // Auto-trigger on word count + time (non-room fallback only).
  useEffect(() => {
    if (isRoomSession) return; // server handles auto-trigger
    if (!recording || !sessionId) return;
    const wc = countWords(transcript);
    const elapsed = (Date.now() - lastSummaryTimeRef.current) / 1000;
    const wordDelta = wc - lastWordsAtSummaryRef.current;
    if (
      wc >= 30 && // never summarize a near-empty transcript
      wordDelta >= wordTriggerDelta &&
      elapsed >= minIntervalSec &&
      !summarizing
    ) {
      const handle = window.setTimeout(() => runClientSummary(), 50);
      return () => window.clearTimeout(handle);
    }
    return undefined;
  }, [
    transcript,
    recording,
    sessionId,
    isRoomSession,
    wordTriggerDelta,
    minIntervalSec,
    runClientSummary,
    summarizing,
  ]);

  // === Unified rendering ===============================================
  //
  // Normalize whichever slice list this mode populated into a shared
  // shape so the rest of the component reads the same way.

  type ViewSlice = {
    id: string;
    text: string;
    createdAt: string;
    wordCount: number;
    triggeredBy?: string;
  };

  const view: ViewSlice[] = useMemo(() => {
    if (isRoomSession) {
      return serverSlices.map((s) => ({
        id: s.id,
        text: s.text,
        createdAt: s.created_at || new Date().toISOString(),
        wordCount: s.word_count || 0,
        triggeredBy: s.triggered_by,
      }));
    }
    return clientSlices.map((s) => ({
      id: s.id,
      text: s.text,
      createdAt: s.createdAt,
      wordCount: s.wordCount,
    }));
  }, [isRoomSession, serverSlices, clientSlices]);

  const latestSliceId = view.length > 0 ? view[view.length - 1].id : null;

  const isExpanded = (id: string) => {
    if (id === latestSliceId) return !expandedIds.has(id);
    return expandedIds.has(id);
  };
  const toggle = (id: string) =>
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const ordered = useMemo(() => [...view].reverse(), [view]);

  const onSummarizeNowClick = isRoomSession ? runServerSlice : runClientSummary;
  const summarizeDisabled = summarizing || (!isRoomSession && !transcript.trim());

  // Pre-recording placeholder for room sessions also shows persisted
  // slices from a previous run if there are any — handy for reviewers
  // landing on a finalized session.
  if (!recording && view.length === 0) {
    return (
      <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          <Sparkles className="h-4 w-4" /> Live summary
        </h2>
        <p className="mt-2 text-sm leading-6 text-zinc-500">
          {isRoomSession
            ? 'No summary slices recorded for this session yet.'
            : 'Auto-summary will start when the room is recording.'}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          <Sparkles className="h-4 w-4" /> Live summary
          {(summarizing || polling) && (
            <Loader2 className="h-3 w-3 animate-spin text-zinc-500" />
          )}
          {isRoomSession && (
            <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0 text-[10px] uppercase tracking-wide text-emerald-200">
              Persisted
            </span>
          )}
        </h2>
        <button
          type="button"
          onClick={onSummarizeNowClick}
          disabled={summarizeDisabled}
          className="inline-flex items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
        >
          <RefreshCw
            className={`h-3 w-3 ${summarizing ? 'animate-spin' : ''}`}
          />
          Summarize now
        </button>
      </div>

      {error && (
        <div className="mb-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {view.length === 0 && !summarizing && (
        <p className="text-sm leading-6 text-zinc-500">
          {isRoomSession
            ? 'Auto-summary will publish a slice once the server reaches the configured word threshold. You can also trigger one now.'
            : `Auto-summary will publish a slice once the transcript reaches ${wordTriggerDelta} new words. You can also trigger one now.`}
        </p>
      )}

      {ordered.length > 0 && (
        <div className="flex flex-col gap-2">
          {ordered.map((slice) => {
            const expanded = isExpanded(slice.id);
            return (
              <div
                key={slice.id}
                className={`rounded-lg border transition-colors ${
                  slice.id === latestSliceId
                    ? 'border-fuchsia-500/40 bg-fuchsia-500/5'
                    : 'border-zinc-800 bg-black/30'
                }`}
              >
                <button
                  type="button"
                  onClick={() => toggle(slice.id)}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-zinc-100 hover:bg-zinc-800/40"
                >
                  <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-400">
                    <span className="text-zinc-300">
                      {clockTime(slice.createdAt)}
                    </span>
                    <span className="text-zinc-600">·</span>
                    <span>{slice.wordCount} words</span>
                    {slice.triggeredBy && (
                      <>
                        <span className="text-zinc-600">·</span>
                        <span className="text-zinc-500">
                          {slice.triggeredBy === 'manual'
                            ? 'manual'
                            : slice.triggeredBy === 'room-recorder'
                            ? 'auto'
                            : slice.triggeredBy}
                        </span>
                      </>
                    )}
                    {slice.id === latestSliceId && (
                      <span className="rounded-full border border-fuchsia-500/40 bg-fuchsia-500/10 px-1.5 py-0 text-[10px] uppercase text-fuchsia-200">
                        Latest
                      </span>
                    )}
                  </div>
                  {expanded ? (
                    <ChevronUp className="h-4 w-4 text-zinc-500" />
                  ) : (
                    <ChevronDown className="h-4 w-4 text-zinc-500" />
                  )}
                </button>
                {expanded && (
                  <div className="border-t border-zinc-800/80 px-3 py-2 text-sm leading-6 text-zinc-100 whitespace-pre-wrap">
                    {slice.text}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
