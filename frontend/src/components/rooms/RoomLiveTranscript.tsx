import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, ScrollText } from 'lucide-react';
import roomsApi, { type LiveSessionPoll } from '../../services/roomsApi';

interface RoomLiveTranscriptProps {
  /** Public session id (UUID) to poll. */
  sessionId: string | null;
  /** Active org slug. */
  orgSlug: string | null;
  /**
   * Whether the room is currently recording. Stops polling when false.
   */
  recording: boolean;
  /** Poll interval. Defaults to 3 s. */
  pollMs?: number;
  /**
   * Optional callback when a new poll finishes — lets the parent
   * surface a word count or last-update timestamp without holding its
   * own poll loop.
   */
  onPoll?: (session: LiveSessionPoll) => void;
}

interface SegmentRow {
  text: string;
  speaker?: string | null;
  start?: number;
  end?: number;
}

/**
 * Polls `/api/recording-sessions/{id}` for the in-progress transcript
 * and renders each segment with optional speaker label. Auto-scrolls to
 * the bottom unless the user has scrolled away. Falls back to the
 * raw `transcript_simple` text when no segments are available yet.
 *
 * Note: the chunks endpoint persists segments into both
 * `transcription_segments` (via the Transcription table) and
 * `transcript_diarized.segments`. We prefer the former because it's
 * already pre-sorted by `start_time` server-side; we fall back to the
 * latter when older sessions only have JSON.
 */
export default function RoomLiveTranscript({
  sessionId,
  orgSlug,
  recording,
  pollMs = 3000,
  onPoll,
}: RoomLiveTranscriptProps) {
  const [session, setSession] = useState<LiveSessionPoll | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const autoScrollRef = useRef(true);
  const onPollRef = useRef(onPoll);
  onPollRef.current = onPoll;

  useEffect(() => {
    if (!recording || !sessionId) {
      setSession(null);
      setError(null);
      return;
    }

    let cancelled = false;
    let timer: number | null = null;

    const tick = async () => {
      if (cancelled) return;
      try {
        setLoading(true);
        const data = await roomsApi.getLiveSession(sessionId, { orgSlug });
        if (cancelled) return;
        setSession(data);
        setError(null);
        if (onPollRef.current) onPollRef.current(data);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Poll failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
      if (!cancelled) {
        timer = window.setTimeout(tick, pollMs);
      }
    };

    tick();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [sessionId, orgSlug, recording, pollMs]);

  const segments = useMemo<SegmentRow[]>(() => {
    if (!session) return [];
    if (session.transcription_segments && session.transcription_segments.length > 0) {
      return session.transcription_segments;
    }
    const seg = session.transcript_diarized?.segments;
    return seg ? (seg as SegmentRow[]) : [];
  }, [session]);

  // Auto-scroll to bottom unless the user has scrolled up. The
  // threshold is intentionally generous (40 px) so a flicker during a
  // re-render doesn't toggle the sticky-bottom behavior.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (autoScrollRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [segments, session?.transcript_simple]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    autoScrollRef.current = atBottom;
  };

  if (!recording) {
    return (
      <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          <ScrollText className="h-4 w-4" /> Live transcript
        </h2>
        <p className="mt-2 text-sm leading-6 text-zinc-500">
          Start the recording to see live transcript here.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          <ScrollText className="h-4 w-4" /> Live transcript
          {loading && session && (
            <Loader2 className="h-3 w-3 animate-spin text-zinc-500" />
          )}
        </h2>
        {session && (
          <div className="text-[11px] text-zinc-500">
            {segments.length || 0} segment{segments.length === 1 ? '' : 's'}
            {session.duration ? ` · ${Math.round(session.duration)}s` : ''}
          </div>
        )}
      </div>

      {error && (
        <div className="mb-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {error} — retrying…
        </div>
      )}

      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="max-h-64 overflow-y-auto rounded-lg bg-black/30 p-3 text-sm leading-6"
        style={{ minHeight: 96 }}
      >
        {!session && (
          <div className="flex items-center gap-2 text-zinc-500">
            <Loader2 className="h-3 w-3 animate-spin" /> Waiting for first
            chunk…
          </div>
        )}
        {session && segments.length === 0 && session.transcript_simple && (
          <p className="text-zinc-200 whitespace-pre-wrap">
            {session.transcript_simple}
          </p>
        )}
        {session && segments.length === 0 && !session.transcript_simple && (
          <div className="text-zinc-500">
            No speech detected yet. Speak into the mic.
          </div>
        )}
        {session && segments.length > 0 && (
          <ol className="flex flex-col gap-1.5">
            {segments.map((seg, idx) => (
              <li
                key={`${seg.start ?? idx}-${idx}`}
                className="flex flex-wrap gap-x-2 text-zinc-100"
              >
                {seg.speaker && (
                  <span className="text-[10px] uppercase tracking-wide text-fuchsia-300">
                    {seg.speaker}
                  </span>
                )}
                {typeof seg.start === 'number' && (
                  <span className="font-mono text-[10px] text-zinc-500">
                    {formatTime(seg.start)}
                  </span>
                )}
                <span className="flex-1 whitespace-pre-wrap">{seg.text}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function formatTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '0:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}
