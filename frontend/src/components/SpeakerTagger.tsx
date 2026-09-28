import React, { useEffect, useMemo, useState } from 'react';
import { Users, Check, X, RefreshCw, AlertCircle, Play, Search, CircleHelp } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';
import { showToast } from './Toast';

interface SpeakerLink {
  id: number;
  session_id: number;
  raw_label: string;
  speaker_id: number | null;
  speaker_name: string | null;
  similarity: number | null;
  source: 'auto' | 'manual';
  confirmed: boolean;
  /**
   * Backend contract (speakers.py): true = a sample was folded into the
   * caller's account voiceprint this request; false = a fold was attempted
   * but skipped (consistency floor / dim guard / no embedding); null or
   * absent = no fold was applicable.
   */
  my_voice_folded?: boolean | null;
  /**
   * Self-introduction name suggestions derived from THIS speaker's own
   * utterances (backend contract: speakers.py SelfIntroName). Only present
   * for unnamed links (speaker_id == null); [] otherwise. Always a
   * suggestion the user confirms — prefills the enroll input, never
   * auto-applies. `evidence` is the source sentence for the chip tooltip.
   */
  name_suggestions?: { name: string; evidence?: string }[];
}

/** Honest fingerprint toast keyed off `my_voice_folded` (see above). */
const toastVoiceFold = (folded: boolean | null | undefined): void => {
  if (folded === true) {
    showToast.success('Your personal voice fingerprint was updated.');
  } else if (folded === false) {
    showToast.warning(
      "Couldn't update your voice fingerprint from this meeting (audio didn't match or no usable sample).",
    );
  }
};

interface SpeakerOption {
  id: number;
  display_name: string;
  has_centroid: boolean;
  sample_count: number;
  /**
   * Backend contract (v3.43.0 "persistent speaker identity"): true = an
   * auto-created profile not yet named by a human (display_name is a stable
   * machine handle like "Speaker 3F2A"). Flagged in the assign dropdown so
   * the user can tell it apart from a confirmed person.
   */
  auto_generated?: boolean;
}

interface SpeakerTaggerProps {
  sessionId: string;
  onChange?: (links: SpeakerLink[]) => void;
  segments?: SpeakerSampleSegment[];
  onPlaySample?: (start: number, end: number) => void;
}

export interface SpeakerSampleSegment {
  start: number;
  end: number;
  text: string;
  speaker?: string;
  raw_label?: string;
}

/**
 * SpeakerTagger — inline UI for confirming or changing the
 * speaker_session_link rows for a single session. Surfaces under the
 * SessionDetails header so users can fix labels without leaving the page.
 */
export const SpeakerTagger: React.FC<SpeakerTaggerProps> = ({
  sessionId,
  onChange,
  segments = [],
  onPlaySample,
}) => {
  const { activeOrganization } = useOrg();
  const [links, setLinks] = useState<SpeakerLink[]>([]);
  const [speakers, setSpeakers] = useState<SpeakerOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [identifying, setIdentifying] = useState(false);
  const [pendingLinkId, setPendingLinkId] = useState<number | null>(null);
  const [redetecting, setRedetecting] = useState(false);
  const [redetectCount, setRedetectCount] = useState<number>(2);
  // null = auto-detect speaker count (no num_speakers hint); use threshold instead.
  const [redetectAuto, setRedetectAuto] = useState<boolean>(false);
  // Per-link "Enroll new speaker" inline form state. When set, the
  // matching link row swaps its dropdown for a name input + Save button
  // that calls the enroll-from-session endpoint to create a
  // SpeakerProfile + voice sample in one step.
  const [enrollingLinkId, setEnrollingLinkId] = useState<number | null>(null);
  const [enrollName, setEnrollName] = useState<string>("");
  const [enrolling, setEnrolling] = useState(false);
  // "This is me" — when checked, the next assign/enroll also updates the
  // current user's personal voice fingerprint (is_me: true in the body).
  // Unchecked by default and reset after a successful send so labeling
  // someone ELSE afterwards can't pollute the user's voiceprint.
  const [isMe, setIsMe] = useState(false);
  // Start in the smallest, highest-value queue: voices without a human name.
  // This is local presentation state only; it never changes a speaker link.
  const [reviewFilter, setReviewFilter] = useState<'unidentified' | 'needs-review' | 'all'>('unidentified');
  const [searchQuery, setSearchQuery] = useState('');


  const orgHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = {};
    if (activeOrganization?.slug) {
      headers['X-MeetingOps-Org'] = activeOrganization.slug;
    }
    return headers;
  };

  const loadAll = async () => {
    setLoading(true);
    setError(null);
    try {
      const [linksRes, speakersRes] = await Promise.all([
        fetch(`${config.apiBaseUrl}/api/sessions/${sessionId}/speaker-links`, { headers: orgHeaders() }),
        fetch(
          `${config.apiBaseUrl}/api/speakers?session_id=${encodeURIComponent(sessionId)}`,
          { headers: orgHeaders() },
        ),
      ]);
      if (linksRes.ok) {
        const data: SpeakerLink[] = await linksRes.json();
        setLinks(data);
        if (onChange) onChange(data);
      } else if (linksRes.status === 404) {
        setLinks([]);
      } else {
        setError(`Failed to load speaker links (HTTP ${linksRes.status}).`);
      }
      if (speakersRes.ok) {
        setSpeakers(await speakersRes.json());
      }
    } catch (e) {
      setError('Network error loading speaker labels.');
    }
    setLoading(false);
  };

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, activeOrganization?.slug]);

  const reidentify = async () => {
    setIdentifying(true);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/sessions/${sessionId}/identify-speakers`,
        { method: 'POST', headers: orgHeaders() },
      );
      if (res.ok) {
        await loadAll();
      } else {
        setError(`Re-identify failed (HTTP ${res.status}).`);
      }
    } catch (e) {
      setError('Network error during re-identify.');
    }
    setIdentifying(false);
  };

  const pollRediarize = async (): Promise<void> => {
    const deadline = Date.now() + 6 * 60 * 1000; // 6 minutes
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 4000));
      try {
        const res = await fetch(
          `${config.apiBaseUrl}/api/sessions/${sessionId}/rediarize-status`,
          { headers: orgHeaders() },
        );
        if (!res.ok) continue;
        const data = await res.json();
        if (data.status === 'done') {
          await loadAll();
          return;
        }
        if (data.status === 'failed') {
          setError(`Re-detect failed: ${data.error || 'unknown error'}`);
          return;
        }
        // queued or running — keep polling
      } catch {
        // transient network blip; just keep polling
      }
    }
    setError('Re-detect timed out after 6 minutes. The job may still be running; refresh shortly.');
  };

  const redetectSpeakers = async () => {
    setRedetecting(true);
    setError(null);
    try {
      // Auto = plain auto-detect ({}). The speaker-svc rejects per-request
      // clustering_threshold overrides (VRAM churn — env-var-only now), so
      // sending one 400s the whole re-detect.
      const body: any = redetectAuto ? {} : { num_speakers: redetectCount };
      const res = await fetch(
        `${config.apiBaseUrl}/api/sessions/${sessionId}/rediarize`,
        {
          method: 'POST',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
      );
      if (res.status === 202 || res.ok) {
        // Async path: backend accepted the job, poll for completion.
        await pollRediarize();
      } else {
        const txt = await res.text();
        setError(`Re-detect failed (HTTP ${res.status}): ${txt.slice(0, 120)}`);
      }
    } catch (e) {
      setError('Network error during re-detect.');
    }
    setRedetecting(false);
  };

  const enrollFromSession = async (link: SpeakerLink, displayName: string) => {
    const name = displayName.trim();
    if (!name) {
      setError('Enter a name for this speaker.');
      return;
    }
    setEnrolling(true);
    setError(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/sessions/${sessionId}/speaker-links/${link.id}/enroll`,
        {
          method: 'POST',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: name, ...(isMe ? { is_me: true } : {}) }),
        },
      );
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        // Re-pull both lists: a new SpeakerProfile may have been created
        // and the link is now confirmed against it.
        await loadAll();
        setEnrollingLinkId(null);
        setEnrollName("");
        toastVoiceFold(data?.my_voice_folded);
        if (isMe) setIsMe(false);
        showToast.info('Summary will update with the new name in about a minute.');
      } else {
        const txt = await res.text();
        setError(`Enroll failed (HTTP ${res.status}): ${txt.slice(0, 160)}`);
      }
    } catch (e) {
      setError('Network error during enrollment.');
    }
    setEnrolling(false);
  };

  const updateLink = async (link: SpeakerLink, speakerId: number | null) => {
    setPendingLinkId(link.id);
    // Only stamp the user's voiceprint when actually assigning a speaker
    // (not when clearing back to Unknown).
    const sendIsMe = isMe && speakerId !== null;
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/sessions/${sessionId}/speaker-links/${link.id}`,
        {
          method: 'PATCH',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ speaker_id: speakerId, confirmed: true, ...(sendIsMe ? { is_me: true } : {}) }),
        },
      );
      if (res.ok) {
        const updated: SpeakerLink = await res.json();
        const next = links.map(l => (l.id === updated.id ? updated : l));
        setLinks(next);
        if (onChange) onChange(next);
        toastVoiceFold(updated.my_voice_folded);
        if (sendIsMe) setIsMe(false);
        if (speakerId !== null) {
          showToast.info('Summary will update with the new name in about a minute.');
        }
      } else {
        setError(`Failed to update label (HTTP ${res.status}).`);
      }
    } catch (e) {
      setError('Network error updating speaker label.');
    }
    setPendingLinkId(null);
  };

  const unidentifiedCount = links.filter(link => link.speaker_id === null).length;
  const needsReviewCount = links.filter(link => !link.confirmed).length;
  const normalizedSearch = searchQuery.trim().toLocaleLowerCase();
  // If every voice is named, an empty unidentified queue should not look like
  // a broken screen. Keep the control selected, but transparently show all
  // voices and explain why in the helper copy below.
  const effectiveFilter = reviewFilter === 'unidentified' && unidentifiedCount === 0
    ? 'all'
    : reviewFilter;
  const visibleLinks = useMemo(() => links
    .filter(link => {
      if (effectiveFilter === 'unidentified' && link.speaker_id !== null) return false;
      if (effectiveFilter === 'needs-review' && link.confirmed) return false;
      if (!normalizedSearch) return true;
      const searchable = [
        link.raw_label,
        link.speaker_name || '',
        ...(link.name_suggestions || []).flatMap(suggestion => [suggestion.name, suggestion.evidence || '']),
      ].join(' ').toLocaleLowerCase();
      return searchable.includes(normalizedSearch);
    })
    // Even in "All voices", keep unresolved identity work at the top.
    .sort((a, b) => {
      const rank = (link: SpeakerLink) => link.speaker_id === null ? 0 : !link.confirmed ? 1 : 2;
      return rank(a) - rank(b) || a.raw_label.localeCompare(b.raw_label);
    }), [links, effectiveFilter, normalizedSearch]);

  if (loading) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-4 text-sm text-gray-500 flex items-center gap-2">
        <RefreshCw className="w-4 h-4 animate-spin" />
        Loading speaker labels…
      </div>
    );
  }

  if (links.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <div className="flex items-center gap-2 text-sm text-gray-500 mb-3">
          <Users className="w-4 h-4" />
          No diarized speakers detected for this session.
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-gray-600">Re-detect with</label>
          <label className="flex items-center gap-1 text-xs text-gray-600">
            <input
              type="checkbox"
              checked={redetectAuto}
              onChange={(e) => setRedetectAuto(e.target.checked)}
              className="h-3 w-3"
            />
            Auto
          </label>
          {!redetectAuto && (
            <>
              <input
                type="number"
                min={1}
                max={24}
                value={redetectCount}
                onChange={(e) => setRedetectCount(Math.max(1, Math.min(24, Number(e.target.value) || 1)))}
                className="w-16 rounded border border-gray-300 px-2 py-1 text-sm"
              />
              <label className="text-xs text-gray-600">speakers</label>
            </>
          )}
          <button
            onClick={redetectSpeakers}
            disabled={redetecting}
            className="text-xs px-3 py-1 rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 flex items-center gap-1"
            title={redetectAuto ? 'Re-run diarization with auto speaker detection' : 'Re-run diarization with this speaker count'}
          >
            {redetecting ? <RefreshCw className="w-3 h-3 animate-spin" /> : null}
            Run
          </button>
        </div>
        {error && (
          <div className="mt-2 text-xs text-red-600 flex items-center gap-1">
            <AlertCircle className="w-3 h-3" />
            {error}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <div className="flex items-center justify-between mb-3 gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
          <Users className="w-4 h-4 text-purple-600" />
          Speaker labels
          <span className="text-xs text-gray-400">({links.length})</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-gray-600">
            <input
              type="checkbox"
              checked={redetectAuto}
              onChange={(e) => setRedetectAuto(e.target.checked)}
              className="h-3 w-3"
            />
            Auto
          </label>
          {!redetectAuto && (
            <input
              type="number"
              min={1}
              max={24}
              value={redetectCount}
              onChange={(e) => setRedetectCount(Math.max(1, Math.min(24, Number(e.target.value) || 1)))}
              className="w-12 rounded border border-gray-300 px-1 py-0.5 text-xs"
              title="Speakers"
            />
          )}
          <button
            onClick={redetectSpeakers}
            disabled={redetecting}
            className="text-xs px-2 py-1 rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 flex items-center gap-1"
            title={redetectAuto ? "Re-run diarization with auto speaker detection" : "Re-run diarization with this speaker count"}
          >
            {redetecting ? <RefreshCw className="w-3 h-3 animate-spin" /> : null}
            Re-detect
          </button>
          <button
            onClick={reidentify}
            disabled={identifying}
            className="text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-50 flex items-center gap-1"
            title="Match existing labels against the speaker library"
          >
            {identifying ? <RefreshCw className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
            Re-identify
          </button>
        </div>
      </div>

      {onPlaySample && segments.length > 0 && (
        <p className="mb-3 text-xs text-gray-500">
          Listen to representative moments, then assign each voice to a person.
          Samples play from this meeting&apos;s existing audio and are not saved as
          separate biometric clips.
        </p>
      )}

      <div className="mb-3 rounded-lg border border-purple-100 bg-purple-50/60 p-2.5">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-1.5" aria-label="Speaker review filter">
            {([
              ['unidentified', `Needs a name (${unidentifiedCount})`],
              ['needs-review', `Needs review (${needsReviewCount})`],
              ['all', `All voices (${links.length})`],
            ] as const).map(([filter, label]) => (
              <button
                key={filter}
                type="button"
                onClick={() => setReviewFilter(filter)}
                aria-pressed={reviewFilter === filter}
                className={`rounded-full px-2.5 py-1 text-xs font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-purple-500 focus:ring-offset-1 ${
                  reviewFilter === filter
                    ? 'bg-purple-600 text-white'
                    : 'bg-white text-purple-800 ring-1 ring-purple-200 hover:bg-purple-100'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <label className="relative block min-w-0 sm:w-64">
            <span className="sr-only">Search speaker labels and name suggestions</span>
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
            <input
              type="search"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Find a voice or name…"
              className="w-full rounded-md border border-purple-200 bg-white py-1.5 pl-8 pr-7 text-xs text-gray-900 placeholder:text-gray-400 focus:border-purple-500 focus:outline-none focus:ring-2 focus:ring-purple-200"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery('')}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-gray-500 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-purple-500"
                aria-label="Clear speaker search"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </label>
        </div>
        <p className="mt-2 flex items-start gap-1.5 text-[11px] leading-4 text-purple-900">
          <CircleHelp className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
          {unidentifiedCount > 0
            ? 'Start with “Needs a name”: listen, then choose an existing person or enroll a new one. Nothing is changed until you select or save.'
            : 'Every detected voice has a name. Use “Needs review” to confirm suggested matches or “All voices” to revisit a label.'}
        </p>
      </div>

      {error && (
        <div className="mb-2 text-xs text-red-600 flex items-center gap-1">
          <AlertCircle className="w-3 h-3" />
          {error}
        </div>
      )}

      <label className="mb-3 flex items-start gap-2 text-xs text-gray-600 cursor-pointer">
        <input
          type="checkbox"
          checked={isMe}
          onChange={(e) => setIsMe(e.target.checked)}
          className="mt-0.5 h-3.5 w-3.5 accent-purple-600"
        />
        <span>This is me — also update my personal voice fingerprint when I label a speaker below</span>
      </label>

      <p className="mb-2 text-xs text-gray-500" aria-live="polite">
        Showing {visibleLinks.length} of {links.length} voice{links.length === 1 ? '' : 's'}
        {effectiveFilter === 'unidentified' ? ' that need a name' : ''}.
      </p>
      <div className="space-y-2">
        {visibleLinks.map(link => {
          const sim = link.similarity ?? null;
          const simBadge = sim !== null ? `${(sim * 100).toFixed(0)}%` : null;
          const isEnrolling = enrollingLinkId === link.id;
          const representativeSamples = segments
            .filter((segment) => {
              const raw = (segment.raw_label || '').trim();
              const named = (segment.speaker || '').trim();
              return (
                Number.isFinite(segment.start) &&
                !!segment.text?.trim() &&
                (
                  raw === link.raw_label ||
                  named === link.raw_label ||
                  (!!link.speaker_name && named === link.speaker_name)
                )
              );
            })
            .sort((a, b) => {
              const score = (segment: SpeakerSampleSegment) => {
                const duration = Math.max(0, segment.end - segment.start);
                const words = segment.text.trim().split(/\s+/).length;
                return Math.min(duration, 12) * 2 + Math.min(words, 30);
              };
              return score(b) - score(a);
            })
            .slice(0, 3);
          return (
            <div key={link.id} className={`rounded-lg border p-3 ${link.speaker_id === null ? 'border-amber-300 bg-amber-50/60' : link.confirmed ? 'border-gray-200 bg-gray-50/70' : 'border-blue-200 bg-blue-50/50'}`}>
            <div className="flex flex-col gap-2 text-sm sm:flex-row sm:items-center sm:gap-3">
              <span className="font-mono text-xs text-gray-500 w-24 truncate" title={link.raw_label}>
                {link.raw_label}
              </span>
              {isEnrolling ? (
                <>
                  <div className="flex-1 flex flex-col gap-1">
                    {/* Self-intro suggestions ("Hi, I'm John") parsed from
                        this speaker's own lines. Clicking only prefills the
                        name input — the user still presses Save, so nothing
                        is auto-applied. */}
                    {(link.name_suggestions?.length ?? 0) > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[11px] text-gray-500">Suggested:</span>
                        {link.name_suggestions!.map((sugg, i) => (
                          <button
                            key={`${sugg.name}-${i}`}
                            type="button"
                            disabled={enrolling}
                            title={sugg.evidence || `Heard them say: ${sugg.name}`}
                            onClick={() => setEnrollName(sugg.name)}
                            className="rounded-full border border-amber-400 bg-amber-50 px-2 py-0.5 text-[11px] text-amber-800 hover:bg-amber-100 disabled:opacity-50"
                          >
                            {sugg.name}
                          </button>
                        ))}
                      </div>
                    )}
                    <input
                      type="text"
                      value={enrollName}
                      placeholder="e.g. Aaron Stransky"
                      autoFocus
                      onChange={(e) => setEnrollName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !enrolling) enrollFromSession(link, enrollName);
                        if (e.key === 'Escape') { setEnrollingLinkId(null); setEnrollName(""); }
                      }}
                      disabled={enrolling}
                      className="w-full rounded border border-purple-400 px-2 py-1 text-sm bg-white text-gray-900 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-purple-500"
                    />
                  </div>
                  <button
                    onClick={() => enrollFromSession(link, enrollName)}
                    disabled={enrolling || !enrollName.trim()}
                    className="text-xs px-2 py-1 rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 flex items-center gap-1 self-start"
                    title="Create speaker profile + enroll voice from this clip"
                  >
                    {enrolling ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
                    Save
                  </button>
                  <button
                    onClick={() => { setEnrollingLinkId(null); setEnrollName(""); }}
                    disabled={enrolling}
                    className="text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-50 self-start"
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  <select
                    aria-label={`Speaker for ${link.raw_label}`}
                    value={link.speaker_id ?? ''}
                    onChange={e => {
                      const v = e.target.value;
                      if (v === '__enroll__') {
                        setEnrollingLinkId(link.id);
                        // Prefill from a self-intro suggestion ("Hi, I'm
                        // John") when one exists — the user still presses
                        // Save (enrollFromSession), so nothing auto-applies.
                        setEnrollName(link.name_suggestions?.[0]?.name ?? "");
                        return;
                      }
                      updateLink(link, v ? Number(v) : null);
                    }}
                    disabled={pendingLinkId === link.id}
                    className="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 text-sm bg-white text-gray-900 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-purple-500"
                  >
                    <option value="">— Unknown —</option>
                    {speakers.map(s => (
                      <option key={s.id} value={s.id}>
                        {s.display_name}
                        {s.auto_generated && ' (unconfirmed)'}
                        {!s.has_centroid && ' (no enrollment)'}
                      </option>
                    ))}
                    <option value="__enroll__">+ Enroll new speaker…</option>
                  </select>
                  {link.confirmed && (
                    <span title="Manually confirmed" className="text-green-600">
                      <Check className="w-4 h-4" />
                    </span>
                  )}
                  {!link.confirmed && link.source === 'auto' && simBadge && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-blue-50 text-blue-700" title="Auto-matched confidence">
                      {simBadge}
                    </span>
                  )}
                  {!link.confirmed && link.source === 'auto' && !simBadge && link.speaker_id === null && (
                    <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 text-amber-800" title="No matching profile was found">
                      no match · needs a name
                    </span>
                  )}
                </>
              )}
            </div>
            {!isEnrolling && (
              <p className={`mt-1.5 text-xs ${link.speaker_id === null ? 'text-amber-800' : link.confirmed ? 'text-green-700' : 'text-blue-700'}`}>
                {link.speaker_id === null
                  ? 'Unidentified voice — select a known person or choose “Enroll new speaker”.'
                  : link.confirmed
                    ? 'Confirmed identity.'
                    : 'Suggested identity — confirm or correct it after listening.'}
              </p>
            )}
            {onPlaySample && representativeSamples.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-2">
                {representativeSamples.map((sample, index) => {
                  const mins = Math.floor(sample.start / 60);
                  const secs = Math.floor(sample.start % 60).toString().padStart(2, '0');
                  const excerpt = sample.text.trim().replace(/\s+/g, ' ');
                  return (
                    <button
                      key={`${sample.start}-${index}`}
                      type="button"
                      onClick={() => onPlaySample(sample.start, sample.end)}
                      className="group inline-flex max-w-full items-center gap-1.5 rounded-full border border-purple-200 bg-white px-2.5 py-1 text-xs text-purple-700 shadow-sm hover:border-purple-400 hover:bg-purple-50"
                      title={`Play ${link.speaker_name || link.raw_label} at ${mins}:${secs}`}
                    >
                      <Play className="h-3 w-3 shrink-0 fill-current" />
                      <span className="font-mono text-[10px] text-purple-500">{mins}:{secs}</span>
                      <span className="max-w-[18rem] truncate text-gray-600">
                        {excerpt}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            </div>
          );
        })}
        {visibleLinks.length === 0 && (
          <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-4 text-center text-sm text-gray-600">
            No voices match this view. Try clearing the search or choose “All voices”.
          </div>
        )}
      </div>
    </div>
  );
};
