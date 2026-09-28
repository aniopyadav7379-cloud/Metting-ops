import React, { useEffect, useRef, useState } from 'react';
import { Users, Plus, Mic, Trash2, Upload, RefreshCw, AlertCircle, X, Search, AlertTriangle, ChevronDown, ChevronRight, Link2, Check, HelpCircle, Sparkles } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';
import { useAuth } from '../contexts/AuthContext';
import { cacheKey, useHydratedState } from '../utils/cachedState';
import { Dialog } from '../components/ui/Dialog';
import ConfirmModal from '../components/ConfirmModal';
import { showToast } from '../components/Toast';

interface SpeakerSummary {
  id: number;
  organization_id: number;
  display_name: string;
  email?: string | null;
  notes?: string | null;
  embedding_dim?: number | null;
  embedding_model?: string | null;
  sample_count: number;
  has_centroid: boolean;
  contact_id?: string | null;
  contact_link_confirmed?: boolean;
  /**
   * Backend contract (v3.43.0 "persistent speaker identity"): true = an
   * auto-created profile that has NOT yet been named by a human. Its
   * `display_name` is a stable machine handle like "Speaker 3F2A". Surface
   * these as "unconfirmed — tap to name" so users know to confirm them.
   */
  auto_generated?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

interface VoiceSample {
  id: number;
  speaker_id: number;
  source: string;
  source_session_id?: number | null;
  embedding_dim: number;
  embedding_model: string;
  duration_seconds?: number | null;
  similarity_to_centroid?: number | null;
  has_audio: boolean;
  created_at?: string | null;
}

interface SpeakerDetail extends SpeakerSummary {
  voice_samples: VoiceSample[];
  session_count: number;
}

interface ContactOpsPerson {
  person_id: string;
  display_name: string;
  email?: string | null;
}

interface SpeakerTopMatch {
  speaker_id: number;
  display_name: string;
  similarity: number;
}

/**
 * Text-derived self-introduction suggestion for an UNIDENTIFIED speaker
 * (backend contract: speakers.py SelfIntroName). The backend scans ONLY
 * this one speaker's own utterances for a first-person intro ("Hi, I'm
 * John") and offers the extracted name. `evidence` is the source sentence
 * for the hover tooltip / audit. This is the text sibling of the
 * voiceprint `top_matches` — always a suggestion the user confirms, never
 * auto-applied.
 */
interface SelfIntroName {
  name: string;
  evidence?: string;
}

interface UnassignedSpeakerLink {
  id: number;
  raw_label: string;
  session_id: string;
  session_pk: number;
  session_title: string;
  session_started_at?: string | null;
  meeting_date?: string | null;
  duration_seconds: number;
  segment_count: number;
  preview: string;
  sample_audio_url?: string | null;
  top_matches: SpeakerTopMatch[];
  // Optional + defaults to [] server-side (backward compatible). Only
  // populated for speaker_id IS NULL rows, which every unassigned row is.
  name_suggestions?: SelfIntroName[];
}

const isAdminRole = (role?: string, isSuperuser?: boolean): boolean => {
  if (isSuperuser) return true;
  return role === 'admin' || role === 'superuser';
};

const formatSeconds = (seconds?: number | null): string => {
  const value = Number(seconds || 0);
  if (value < 60) return `${value.toFixed(0)}s`;
  const mins = Math.floor(value / 60);
  const secs = Math.round(value % 60);
  return `${mins}m ${secs}s`;
};

const formatDate = (value?: string | null): string => {
  if (!value) return 'No date';
  return new Date(value).toLocaleDateString();
};

export const SpeakerLibrary: React.FC = () => {
  const { user } = useAuth();
  const { activeOrganization } = useOrg();

  const isAdmin = isAdminRole(activeOrganization?.role, user?.is_superuser);

  // Speaker list hydrates from cache so the admin sidebar populates
  // immediately on refresh instead of flashing through "Loading…".
  const [speakers, setSpeakers, speakersHydrated] = useHydratedState<SpeakerSummary[]>(
    cacheKey('speakers', activeOrganization?.slug),
    [],
  );
  const [loading, setLoading] = useState(!speakersHydrated);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<SpeakerDetail | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [newNotes, setNewNotes] = useState('');
  const [enrolling, setEnrolling] = useState(false);
  const [unassigned, setUnassigned] = useState<UnassignedSpeakerLink[]>([]);
  const [unassignedTotal, setUnassignedTotal] = useState(0);
  // Backend returns `next_offset` when there are more rows beyond what was
  // returned (see backend/api/speakers.py UnassignedSpeakerLinksPage). Track
  // it so the "Load more" button can fetch the next page without trampling
  // what's already on screen. Codex audit flagged the original
  // `limit=50, no pagination` as a silent ceiling.
  const [unassignedNextOffset, setUnassignedNextOffset] = useState<number | null>(null);
  const UNASSIGNED_PAGE_SIZE = 50;
  const [loadingUnassigned, setLoadingUnassigned] = useState(false);
  const [loadingMoreUnassigned, setLoadingMoreUnassigned] = useState(false);
  // Collapsible + height-capped so a long unassigned list can't push the
  // enrolled-speakers grid below the fold.
  const [unassignedOpen, setUnassignedOpen] = useState(true);
  // v3.19 (audit §6). Replace `window.confirm()` on destructive flows
  // with our themed ConfirmModal so screen readers, focus management,
  // and the Cancel-as-default-focus convention apply uniformly.
  const [pendingDeleteSampleId, setPendingDeleteSampleId] = useState<number | null>(null);
  const [pendingDeleteSpeakerId, setPendingDeleteSpeakerId] = useState<number | null>(null);
  const [assigningLinkId, setAssigningLinkId] = useState<number | null>(null);
  const [assignDialogLink, setAssignDialogLink] = useState<UnassignedSpeakerLink | null>(null);
  const [speakerSearch, setSpeakerSearch] = useState('');
  const [manualSpeakerId, setManualSpeakerId] = useState<number | null>(null);
  const [createAssignName, setCreateAssignName] = useState('');
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeTargetId, setMergeTargetId] = useState<number | null>(null);
  const [mergeConfirm, setMergeConfirm] = useState('');
  const [merging, setMerging] = useState(false);
  const [contactQuery, setContactQuery] = useState('');
  const [contactResults, setContactResults] = useState<ContactOpsPerson[]>([]);
  const [contactSearching, setContactSearching] = useState(false);
  const [contactLinking, setContactLinking] = useState(false);
  // v3.43.0: re-summarize a speaker's past meetings (regenerates every AI
  // summary this speaker appears in). Tracks the in-flight speaker id so the
  // button can show a spinner + disable while the enqueue request is open.
  const [resummarizingId, setResummarizingId] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const orgHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = {};
    if (activeOrganization?.slug) {
      headers['X-MeetingOps-Org'] = activeOrganization.slug;
    }
    return headers;
  };

  const loadSpeakers = async () => {
    // Only show the "Loading…" sidebar state when we have nothing to
    // show. Once the list is populated (from cache or a previous fetch)
    // we refresh in place — no spinner takeover.
    if (speakers.length === 0) setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers`, { headers: orgHeaders() });
      if (res.ok) {
        const data: SpeakerSummary[] = await res.json();
        setSpeakers(data);
        if (selectedId && !data.some(s => s.id === selectedId)) {
          setSelectedId(null);
          setDetail(null);
        }
      } else {
        setError(`Failed to load speakers (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error loading speakers.');
    }
    setLoading(false);
  };

  const loadUnassigned = async () => {
    if (!isAdmin) return;
    setLoadingUnassigned(true);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/speakers/unassigned-links?limit=${UNASSIGNED_PAGE_SIZE}&offset=0`,
        { headers: orgHeaders() },
      );
      if (res.ok) {
        const data = await res.json();
        setUnassigned(Array.isArray(data.items) ? data.items : []);
        setUnassignedTotal(Number(data.total || 0));
        const nextRaw = data.next_offset;
        setUnassignedNextOffset(typeof nextRaw === 'number' ? nextRaw : null);
      } else {
        setError(`Failed to load unassigned fingerprints (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error loading unassigned fingerprints.');
    } finally {
      setLoadingUnassigned(false);
    }
  };

  const loadMoreUnassigned = async () => {
    if (!isAdmin) return;
    if (unassignedNextOffset === null) return;
    setLoadingMoreUnassigned(true);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/speakers/unassigned-links?limit=${UNASSIGNED_PAGE_SIZE}&offset=${unassignedNextOffset}`,
        { headers: orgHeaders() },
      );
      if (res.ok) {
        const data = await res.json();
        const incoming: UnassignedSpeakerLink[] = Array.isArray(data.items) ? data.items : [];
        // Append while de-duping by link id — the page below might have
        // moved if an assign landed between calls.
        setUnassigned(prev => {
          const seen = new Set(prev.map(x => x.id));
          return [...prev, ...incoming.filter(x => !seen.has(x.id))];
        });
        setUnassignedTotal(Number(data.total || 0));
        const nextRaw = data.next_offset;
        setUnassignedNextOffset(typeof nextRaw === 'number' ? nextRaw : null);
      } else {
        setError(`Failed to load more fingerprints (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error loading more fingerprints.');
    } finally {
      setLoadingMoreUnassigned(false);
    }
  };

  const loadDetail = async (id: number) => {
    setSelectedId(id);
    setDetail(null);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers/${id}`, { headers: orgHeaders() });
      if (res.ok) {
        setDetail(await res.json());
      }
    } catch {
      // non-fatal — leave detail null and surface in list reload
    }
  };

  useEffect(() => {
    if (!isAdmin) {
      // Non-admins never see speakers — skip the network call entirely.
      setLoading(false);
      return;
    }
    loadSpeakers();
    loadUnassigned();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeOrganization?.slug, isAdmin]);


  useEffect(() => {
    const q = contactQuery.trim();
    if (!detail || q.length < 2) {
      setContactResults([]);
      setContactSearching(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setContactSearching(true);
      try {
        const res = await fetch(
          `${config.apiBaseUrl}/api/contact-ops/people/search?q=${encodeURIComponent(q)}&limit=8`,
          { headers: orgHeaders(), signal: controller.signal },
        );
        if (!res.ok) {
          setContactResults([]);
          return;
        }
        const data = await res.json();
        setContactResults(Array.isArray(data.items) ? data.items : []);
      } catch (e) {
        if (!(e instanceof DOMException && e.name === 'AbortError')) {
          setError('Contact-Ops search failed.');
        }
      } finally {
        setContactSearching(false);
      }
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [contactQuery, detail?.id, activeOrganization?.slug]);

  const assignUnassignedLink = async (link: UnassignedSpeakerLink, speakerId: number) => {
    setAssigningLinkId(link.id);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/sessions/${link.session_id}/speaker-links/${link.id}`,
        {
          method: 'PATCH',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ speaker_id: speakerId, confirmed: true }),
        },
      );
      if (res.ok) {
        setUnassigned(prev => prev.filter(item => item.id !== link.id));
        setUnassignedTotal(prev => Math.max(0, prev - 1));
        setSpeakers(prev => prev.map(s => (
          s.id === speakerId ? { ...s, sample_count: (s.sample_count || 0) + 1 } : s
        )));
        setAssignDialogLink(null);
        setManualSpeakerId(null);
        setSpeakerSearch('');
        showToast.info('Summary will update with the new name in about a minute.');
        if (selectedId === speakerId) await loadDetail(speakerId);
      } else {
        setError(`Assign failed (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error assigning speaker.');
    } finally {
      setAssigningLinkId(null);
    }
  };

  const createSpeakerAndAssign = async () => {
    if (!assignDialogLink || !createAssignName.trim()) return;
    setAssigningLinkId(assignDialogLink.id);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers`, {
        method: 'POST',
        headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: createAssignName.trim() }),
      });
      if (!res.ok) {
        setError(`Create speaker failed (HTTP ${res.status}).`);
        return;
      }
      const created: SpeakerSummary = await res.json();
      setSpeakers(prev => [...prev, created].sort((a, b) => a.display_name.localeCompare(b.display_name)));
      setCreateAssignName('');
      await assignUnassignedLink(assignDialogLink, created.id);
    } catch {
      setError('Network error creating speaker.');
    } finally {
      setAssigningLinkId(null);
    }
  };

  const mergeSelectedSpeaker = async () => {
    if (!detail || !mergeTargetId) return;
    setMerging(true);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers/${mergeTargetId}/merge`, {
        method: 'POST',
        headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_id: detail.id }),
      });
      if (res.ok) {
        const merged: SpeakerSummary = await res.json();
        setMergeOpen(false);
        setMergeTargetId(null);
        setMergeConfirm('');
        await loadSpeakers();
        await loadDetail(merged.id);
      } else {
        setError(`Merge failed (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error merging speakers.');
    } finally {
      setMerging(false);
    }
  };

  const createSpeaker = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers`, {
        method: 'POST',
        headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          display_name: newName.trim(),
          email: newEmail.trim() || null,
          notes: newNotes.trim() || null,
        }),
      });
      if (res.ok) {
        const created: SpeakerSummary = await res.json();
        setShowCreate(false);
        setNewName('');
        setNewEmail('');
        setNewNotes('');
        await loadSpeakers();
        loadDetail(created.id);
      } else if (res.status === 409) {
        setError('A speaker with that name already exists.');
      } else {
        setError(`Create failed (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error creating speaker.');
    }
    setCreating(false);
  };


  const updateContactLink = async (contactId: string | null) => {
    if (!detail) return;
    setContactLinking(true);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers/${detail.id}/contact-link`, {
        method: 'PATCH',
        headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ contact_id: contactId, confirmed: Boolean(contactId) }),
      });
      if (!res.ok) {
        setError(`Contact link update failed (HTTP ${res.status}).`);
        return;
      }
      const updated: SpeakerSummary = await res.json();
      setDetail(prev => prev ? { ...prev, ...updated } : prev);
      setSpeakers(prev => prev.map(s => s.id === updated.id ? { ...s, ...updated } : s));
      setContactQuery('');
      setContactResults([]);
    } catch {
      setError('Network error updating contact link.');
    } finally {
      setContactLinking(false);
    }
  };

  const enrollSample = async (file: File) => {
    if (!selectedId) return;
    setEnrolling(true);
    try {
      const form = new FormData();
      form.append('audio', file);
      const res = await fetch(`${config.apiBaseUrl}/api/speakers/${selectedId}/voice-samples`, {
        method: 'POST',
        headers: orgHeaders(),
        body: form,
      });
      if (res.ok) {
        await loadSpeakers();
        await loadDetail(selectedId);
      } else if (res.status === 502) {
        setError('Speaker service unreachable. Ask an operator to check meet-speaker-svc.');
      } else {
        setError(`Enrollment failed (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error during enrollment upload.');
    }
    setEnrolling(false);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const deleteSample = async (sampleId: number) => {
    if (!selectedId) return;
    // Confirm path moved to <ConfirmModal>. Callers set
    // `pendingDeleteSampleId` and the modal's onConfirm invokes this.
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/speakers/${selectedId}/voice-samples/${sampleId}`,
        { method: 'DELETE', headers: orgHeaders() },
      );
      if (res.ok) {
        await loadSpeakers();
        await loadDetail(selectedId);
      }
    } catch {
      setError('Network error deleting voice sample.');
    }
  };

  const deleteSpeaker = async (id: number) => {
    // Confirm path moved to <ConfirmModal>.
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/speakers/${id}`, {
        method: 'DELETE',
        headers: orgHeaders(),
      });
      if (res.ok) {
        if (selectedId === id) {
          setSelectedId(null);
          setDetail(null);
        }
        await loadSpeakers();
      }
    } catch {
      setError('Network error deleting speaker.');
    }
  };

  // v3.43.0 — POST /api/speakers/{id}/resummarize-history (no body).
  // Regenerates the AI summary of every past meeting this speaker appears
  // in. Most useful right after naming an auto-generated speaker so old
  // summaries pick up the real name. Returns sessions_total +
  // resummaries_enqueued; we surface the enqueued count via the toast.
  const resummarizeHistory = async (id: number) => {
    setResummarizingId(id);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/speakers/${id}/resummarize-history`,
        { method: 'POST', headers: orgHeaders() },
      );
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        const count = Number(data?.resummaries_enqueued ?? 0);
        if (count > 0) {
          showToast.info(
            `Re-summarizing ${count} past meeting${count === 1 ? '' : 's'}…`,
          );
        } else {
          showToast.info('No past meetings needed re-summarizing.');
        }
      } else {
        setError(`Re-summarize failed (HTTP ${res.status}).`);
      }
    } catch {
      setError('Network error re-summarizing past meetings.');
    } finally {
      setResummarizingId(null);
    }
  };

  const filteredSpeakers = speakers.filter(s =>
    s.display_name.toLowerCase().includes(speakerSearch.trim().toLowerCase())
  );
  const mergeCandidates = speakers.filter(s => s.id !== detail?.id);
  const selectedMergeTarget = mergeCandidates.find(s => s.id === mergeTargetId) || null;
  const mergeConfirmMatches = Boolean(
    detail && mergeConfirm.trim().toLowerCase() === detail.display_name.trim().toLowerCase()
  );

  if (!isAdmin) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-center p-8 text-zinc-300">
        <Users className="w-10 h-10 text-zinc-600 mb-3" />
        <h1 className="text-lg font-semibold text-zinc-100 mb-2">Speaker Library</h1>
        <p className="text-sm text-zinc-400 max-w-md">
          Speaker enrollment is restricted to organisation admins. Ask your workspace admin
          to enroll voices, then identification will run automatically on every recording.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-zinc-950 p-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Users className="w-5 h-5 text-fuchsia-400" />
          <h1 className="text-lg font-semibold text-zinc-100">Speaker Library</h1>
          <span className="text-xs text-zinc-500">({speakers.length} enrolled)</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              loadSpeakers();
              loadUnassigned();
            }}
            className="text-xs px-2 py-1 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-900 flex items-center gap-1"
          >
            <RefreshCw className="w-3 h-3" />
            Refresh
          </button>
          <button
            onClick={() => setShowCreate(true)}
            className="text-xs px-3 py-1.5 rounded bg-fuchsia-600 hover:bg-fuchsia-500 text-white flex items-center gap-1"
          >
            <Plus className="w-3 h-3" />
            New speaker
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 text-xs text-red-300 bg-red-950/50 border border-red-900 rounded px-3 py-2 flex items-center gap-2">
          <AlertCircle className="w-4 h-4" />
          {error}
          <button onClick={() => setError(null)} className="ml-auto text-red-300 hover:text-white">
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

      <section className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900 p-3">
        <button
          type="button"
          onClick={() => setUnassignedOpen(v => !v)}
          aria-expanded={unassignedOpen}
          className="mb-3 flex w-full items-center justify-between gap-2 text-left"
        >
          <div className="flex items-center gap-2">
            {unassignedOpen
              ? <ChevronDown className="h-4 w-4 text-zinc-500" />
              : <ChevronRight className="h-4 w-4 text-zinc-500" />}
            <Mic className="h-4 w-4 text-fuchsia-400" />
            <h2 className="text-sm font-semibold text-zinc-100">Unassigned voice fingerprints</h2>
            <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
              {unassignedTotal}
            </span>
          </div>
          {loadingUnassigned && (
            <RefreshCw className="h-4 w-4 animate-spin text-zinc-500" />
          )}
        </button>
        {unassignedOpen && (unassigned.length === 0 ? (
          <div className="text-xs text-zinc-500">
            No unassigned fingerprints in this workspace.
          </div>
        ) : (
          // Height-cap with internal scroll so the enrolled-speakers grid below
          // is always reachable, regardless of how many unassigned rows there are.
          <div className="max-h-96 space-y-2 overflow-y-auto pr-1">
            {unassigned.map(link => (
              <div key={link.id} className="rounded border border-zinc-800 bg-zinc-950/60 p-3">
                <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-zinc-100">{link.session_title}</span>
                      <span className="text-xs text-zinc-500">{formatDate(link.session_started_at || link.meeting_date)}</span>
                      <span className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-fuchsia-200">
                        {link.raw_label}
                      </span>
                      <span className="text-xs text-zinc-500">
                        {formatSeconds(link.duration_seconds)} · {link.segment_count} segment{link.segment_count === 1 ? '' : 's'}
                      </span>
                    </div>
                    {link.preview && (
                      <p className="mt-1 text-xs text-zinc-400">“{link.preview}”</p>
                    )}
                    {link.top_matches.length > 0 && (
                      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                        <span className="text-zinc-500">Looks like:</span>
                        {link.top_matches.map(match => (
                          <button
                            key={match.speaker_id}
                            type="button"
                            disabled={assigningLinkId === link.id}
                            onClick={() => assignUnassignedLink(link, match.speaker_id)}
                            className="rounded-full border border-fuchsia-500/40 bg-fuchsia-500/10 px-2 py-0.5 text-fuchsia-100 hover:bg-fuchsia-500/20 disabled:opacity-50"
                          >
                            {match.display_name} {match.similarity.toFixed(2)}
                          </button>
                        ))}
                      </div>
                    )}
                    {/* Text-derived self-intro suggestions ("Hi, I'm John").
                        Distinct amber accent so "heard them say their name"
                        reads differently from a fuchsia voiceprint match.
                        Clicking does NOT assign — it opens the existing
                        Assign dialog with the name prefilled so the user
                        still confirms (Create + assign). */}
                    {(link.name_suggestions?.length ?? 0) > 0 && (
                      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                        <span className="text-zinc-500">Heard them introduce themselves:</span>
                        {link.name_suggestions!.map((sugg, i) => (
                          <button
                            key={`${sugg.name}-${i}`}
                            type="button"
                            disabled={assigningLinkId === link.id}
                            title={sugg.evidence || `Suggested from this speaker's own words: ${sugg.name}`}
                            onClick={() => {
                              setAssignDialogLink(link);
                              setManualSpeakerId(null);
                              setSpeakerSearch('');
                              setCreateAssignName(sugg.name);
                            }}
                            className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-amber-100 hover:bg-amber-500/20 disabled:opacity-50"
                          >
                            Suggested: {sugg.name}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      setAssignDialogLink(link);
                      setManualSpeakerId(link.top_matches[0]?.speaker_id ?? null);
                      setSpeakerSearch('');
                      // Prefill the create-new-speaker name from a self-intro
                      // suggestion when there's no voiceprint match to pick —
                      // the most common one-click path lands pre-filled but
                      // still requires the user to press "Create + assign".
                      setCreateAssignName(
                        link.top_matches.length === 0 ? (link.name_suggestions?.[0]?.name ?? '') : '',
                      );
                    }}
                    className="shrink-0 rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800"
                  >
                    Assign…
                  </button>
                </div>
              </div>
            ))}
            {/* Pagination — the backend pages 50 at a time
                (UnassignedSpeakerLinksPage.next_offset). Earlier versions
                ignored that field, so any org with >50 unassigned rows
                showed a silent ceiling. */}
            {unassignedNextOffset !== null && (
              <div className="pt-2">
                <button
                  type="button"
                  onClick={loadMoreUnassigned}
                  disabled={loadingMoreUnassigned}
                  className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                >
                  {loadingMoreUnassigned ? (
                    <span className="inline-flex items-center justify-center gap-2">
                      <RefreshCw className="h-3 w-3 animate-spin" />
                      Loading…
                    </span>
                  ) : (
                    <>
                      Load more
                      <span className="ml-2 text-[10px] text-zinc-500">
                        ({unassigned.length} of {unassignedTotal})
                      </span>
                    </>
                  )}
                </button>
              </div>
            )}
          </div>
        ))}
      </section>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 flex-1 overflow-hidden">
        {/* Left column: speaker list */}
        <aside className="md:col-span-1 overflow-y-auto bg-zinc-900 border border-zinc-800 rounded-lg p-2">
          {loading ? (
            <div className="text-sm text-zinc-500 flex items-center gap-2 p-3">
              <RefreshCw className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : speakers.length === 0 ? (
            <div className="text-sm text-zinc-500 p-3">
              No speakers yet. Click <span className="text-fuchsia-400">New speaker</span> to enroll one.
            </div>
          ) : (
            <ul className="space-y-1">
              {speakers.map(s => (
                <li key={s.id}>
                  <button
                    onClick={() => loadDetail(s.id)}
                    className={`w-full text-left px-3 py-2 rounded transition-colors ${
                      selectedId === s.id
                        ? 'bg-zinc-800 text-zinc-100'
                        : 'text-zinc-300 hover:bg-zinc-800/50'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium truncate">{s.display_name}</span>
                      {s.has_centroid && (
                        <Mic className="w-3 h-3 shrink-0 text-emerald-400" />
                      )}
                    </div>
                    {/* v3.43.0: auto-created profiles whose name is still a
                        machine handle ("Speaker 3F2A"). Surface a muted
                        "tap to name" cue so users know to confirm them. */}
                    {s.auto_generated && (
                      <span className="mt-1 inline-flex items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 text-[11px] text-amber-300">
                        <HelpCircle className="h-3 w-3" />
                        Unconfirmed · tap to name
                      </span>
                    )}
                    <div className="text-xs text-zinc-500 mt-0.5">
                      {s.sample_count} sample{s.sample_count === 1 ? '' : 's'}
                      {s.email ? ` · ${s.email}` : ''}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        {/* Right column: detail */}
        <section className="md:col-span-2 overflow-y-auto bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          {!detail ? (
            <div className="text-sm text-zinc-500">Pick a speaker to view enrollment samples.</div>
          ) : (
            <>
              <div className="flex items-start justify-between mb-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-base font-semibold text-zinc-100">{detail.display_name}</h2>
                    {/* v3.43.0: this profile is auto-created and still
                        carries a machine handle — prompt the user to rename
                        it (the existing rename flow auto-relabels history). */}
                    {detail.auto_generated && (
                      <span className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300">
                        <HelpCircle className="h-3 w-3" />
                        Unconfirmed — name this speaker
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-zinc-500 mt-0.5">
                    {detail.email || 'No email on file'} · {detail.session_count} meeting
                    {detail.session_count === 1 ? '' : 's'} linked
                  </div>
                  {detail.notes && (
                    <p className="text-xs text-zinc-400 mt-2 max-w-xl whitespace-pre-wrap">{detail.notes}</p>
                  )}
                </div>
                <div className="flex flex-wrap justify-end gap-2">
                  {/* v3.43.0: regenerate the AI summaries of every past
                      meeting this speaker appears in. Only meaningful once
                      the speaker has linked sessions; especially useful right
                      after naming an auto-generated speaker. */}
                  {detail.session_count > 0 && (
                    <button
                      onClick={() => resummarizeHistory(detail.id)}
                      disabled={resummarizingId === detail.id}
                      className="text-xs px-2 py-1 rounded border border-fuchsia-800 text-fuchsia-300 hover:bg-fuchsia-950/40 disabled:opacity-50 flex items-center gap-1"
                      title="Regenerate the AI summary of every past meeting this speaker appears in"
                    >
                      {resummarizingId === detail.id ? (
                        <RefreshCw className="w-3 h-3 animate-spin" />
                      ) : (
                        <Sparkles className="w-3 h-3" />
                      )}
                      Re-summarize past meetings
                    </button>
                  )}
                  <button
                    onClick={() => {
                      setMergeOpen(true);
                      setMergeTargetId(null);
                      setMergeConfirm('');
                    }}
                    className="text-xs px-2 py-1 rounded border border-rose-900 text-rose-300 hover:bg-rose-950/40 flex items-center gap-1"
                  >
                    <AlertTriangle className="w-3 h-3" />
                    Merge into another speaker…
                  </button>
                  <button
                    onClick={() => setPendingDeleteSpeakerId(detail.id)}
                    className="text-xs px-2 py-1 rounded border border-red-900 text-red-300 hover:bg-red-950/40 flex items-center gap-1"
                    aria-label="Delete speaker"
                  >
                    <Trash2 className="w-3 h-3" />
                    Delete
                  </button>
                </div>
              </div>

              <div className="mb-4 rounded border border-zinc-800 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-xs uppercase tracking-wide text-zinc-500">Contact-Ops link</div>
                  {detail.contact_id && detail.contact_link_confirmed && (
                    <span className="inline-flex items-center gap-1 rounded bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300">
                      <Check className="h-3 w-3" />
                      Confirmed
                    </span>
                  )}
                </div>
                {detail.contact_id ? (
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 text-xs text-zinc-400">
                      <span className="mr-2 text-zinc-500">person_id</span>
                      <code className="break-all text-zinc-200">{detail.contact_id}</code>
                    </div>
                    <button
                      type="button"
                      onClick={() => updateContactLink(null)}
                      disabled={contactLinking}
                      className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                    >
                      Clear link
                    </button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="relative">
                      <Search className="pointer-events-none absolute left-2 top-2 h-3.5 w-3.5 text-zinc-500" />
                      <input
                        type="text"
                        value={contactQuery}
                        onChange={e => setContactQuery(e.target.value)}
                        placeholder="Search Contact-Ops by name"
                        className="w-full rounded border border-zinc-700 bg-zinc-950 py-1.5 pl-7 pr-2 text-sm text-zinc-100 outline-none focus:border-fuchsia-500"
                      />
                    </div>
                    {contactSearching && <div className="text-xs text-zinc-500">Searching…</div>}
                    {contactResults.length > 0 && (
                      <div className="max-h-44 overflow-y-auto rounded border border-zinc-800 bg-zinc-950">
                        {contactResults.map(person => (
                          <button
                            key={person.person_id}
                            type="button"
                            onClick={() => updateContactLink(person.person_id)}
                            disabled={contactLinking}
                            className="block w-full px-2 py-2 text-left text-sm hover:bg-zinc-800 disabled:opacity-50"
                          >
                            <span className="block truncate font-medium text-zinc-100">{person.display_name}</span>
                            {person.email && <span className="block truncate text-xs text-zinc-500">{person.email}</span>}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="border border-zinc-800 rounded p-3 mb-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="text-xs uppercase tracking-wide text-zinc-500">Enrollment samples</div>
                  <div>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept="audio/*"
                      className="hidden"
                      onChange={e => {
                        const file = e.target.files?.[0];
                        if (file) enrollSample(file);
                      }}
                    />
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      disabled={enrolling}
                      className="text-xs px-3 py-1.5 rounded bg-fuchsia-600 hover:bg-fuchsia-500 text-white disabled:opacity-50 flex items-center gap-1"
                    >
                      {enrolling ? (
                        <>
                          <RefreshCw className="w-3 h-3 animate-spin" /> Embedding…
                        </>
                      ) : (
                        <>
                          <Upload className="w-3 h-3" /> Upload sample
                        </>
                      )}
                    </button>
                  </div>
                </div>

                {detail.voice_samples.length === 0 ? (
                  <div className="text-xs text-zinc-500">
                    Upload at least one 3–30s clean speech clip so this speaker can be identified.
                  </div>
                ) : (
                  <ul className="divide-y divide-zinc-800">
                    {detail.voice_samples.map(sample => (
                      <li key={sample.id} className="py-2 flex items-center gap-3 text-sm">
                        <Mic className="w-3 h-3 text-emerald-400" />
                        <span className="flex-1 text-zinc-300">
                          {sample.source === 'enrollment' ? 'Enrollment' : 'From meeting'}
                          {sample.source_session_id ? ` (session #${sample.source_session_id})` : ''}
                        </span>
                        <span className="text-xs text-zinc-500">
                          {sample.duration_seconds ? `${sample.duration_seconds.toFixed(1)}s` : '—'}
                          {sample.similarity_to_centroid !== null && sample.similarity_to_centroid !== undefined
                            ? ` · ${(sample.similarity_to_centroid * 100).toFixed(0)}%`
                            : ''}
                        </span>
                        <button
                          onClick={() => setPendingDeleteSampleId(sample.id)}
                          className="text-zinc-400 hover:text-red-300"
                          aria-label="Delete sample"
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="text-xs text-zinc-500">
                Embedding model: <code className="text-zinc-300">{detail.embedding_model || 'not yet set'}</code>
                {detail.embedding_dim ? ` · ${detail.embedding_dim}-d` : ''}
              </div>
            </>
          )}
        </section>
      </div>

      {/* v3.19 (audit §6). Speaker Library modals migrated off ad-hoc
          `fixed inset-0` overlays onto Radix Dialog so focus trap,
          focus return, role=dialog, aria-modal, ESC, and click-outside
          all work consistently with the rest of the app. */}
      <Dialog
        open={assignDialogLink !== null}
        onOpenChange={(open) => { if (!open) setAssignDialogLink(null); }}
      >
        <Dialog.Content size="lg" describedById="assign-fingerprint-description">
          <Dialog.Header>
            <div className="flex-1">
              <Dialog.Title>Assign fingerprint</Dialog.Title>
              {assignDialogLink && (
                <p id="assign-fingerprint-description" className="mt-1 text-xs text-zinc-500">
                  {assignDialogLink.raw_label} in {assignDialogLink.session_title}
                </p>
              )}
            </div>
            <Dialog.Close />
          </Dialog.Header>
          <div className="px-5 py-4">
            <div className="relative mb-3">
              <Search className="absolute left-3 top-2.5 h-4 w-4 text-zinc-500" />
              <input
                value={speakerSearch}
                onChange={e => setSpeakerSearch(e.target.value)}
                placeholder="Search enrolled speakers"
                className="w-full rounded bg-zinc-950 border border-zinc-700 py-2 pl-9 pr-3 text-sm text-zinc-100"
              />
            </div>

            <div className="max-h-52 overflow-y-auto rounded border border-zinc-800 mb-4">
              {filteredSpeakers.length === 0 ? (
                <div className="p-3 text-xs text-zinc-500">No matching enrolled speakers.</div>
              ) : (
                filteredSpeakers.map(s => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => setManualSpeakerId(s.id)}
                    className={`w-full px-3 py-2 text-left text-sm hover:bg-zinc-800 ${
                      manualSpeakerId === s.id ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-300'
                    }`}
                  >
                    {s.display_name}
                    <span className="ml-2 text-xs text-zinc-500">{s.sample_count} samples</span>
                  </button>
                ))
              )}
            </div>

            <div className="rounded border border-zinc-800 p-3">
              <label className="block text-xs text-zinc-400 mb-1">
                Create new speaker + assign
              </label>
              <div className="flex gap-2">
                <input
                  value={createAssignName}
                  onChange={e => setCreateAssignName(e.target.value)}
                  placeholder="New speaker name"
                  className="flex-1 rounded bg-zinc-950 border border-zinc-700 px-3 py-2 text-sm text-zinc-100"
                />
                <button
                  type="button"
                  onClick={createSpeakerAndAssign}
                  disabled={!createAssignName.trim() || (assignDialogLink !== null && assigningLinkId === assignDialogLink.id)}
                  className="rounded bg-fuchsia-600 px-3 py-2 text-xs text-white hover:bg-fuchsia-500 disabled:opacity-50"
                >
                  Create + assign
                </button>
              </div>
            </div>
          </div>
          <Dialog.Footer>
            <button
              onClick={() => setAssignDialogLink(null)}
              className="text-xs px-3 py-1.5 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800"
            >
              Cancel
            </button>
            <button
              onClick={() => assignDialogLink && manualSpeakerId && assignUnassignedLink(assignDialogLink, manualSpeakerId)}
              disabled={!manualSpeakerId || (assignDialogLink !== null && assigningLinkId === assignDialogLink.id)}
              className="text-xs px-3 py-1.5 rounded bg-fuchsia-600 hover:bg-fuchsia-500 text-white disabled:opacity-50"
            >
              {assignDialogLink && assigningLinkId === assignDialogLink.id ? 'Assigning…' : 'Assign selected'}
            </button>
          </Dialog.Footer>
        </Dialog.Content>
      </Dialog>

      <Dialog
        open={mergeOpen && detail !== null}
        onOpenChange={(open) => { if (!open) setMergeOpen(false); }}
      >
        <Dialog.Content
          size="lg"
          describedById="merge-speaker-description"
          contentClassName="border-rose-900"
        >
          <Dialog.Header>
            <div className="flex items-start gap-3 flex-1">
              <AlertTriangle className="w-5 h-5 text-rose-300 mt-0.5 shrink-0" />
              <div className="flex-1">
                <Dialog.Title>Merge speaker</Dialog.Title>
                {detail && (
                  <p id="merge-speaker-description" className="mt-1 text-xs text-rose-200">
                    This will permanently delete {detail.display_name} after moving its samples
                    and session links into the selected speaker. Cannot be undone.
                  </p>
                )}
              </div>
            </div>
            <Dialog.Close />
          </Dialog.Header>
          <div className="px-5 py-4">
            <label className="block text-xs text-zinc-400 mb-1">Merge into</label>
            <select
              value={mergeTargetId ?? ''}
              onChange={e => setMergeTargetId(e.target.value ? Number(e.target.value) : null)}
              className="w-full px-3 py-2 rounded bg-zinc-950 border border-zinc-700 text-sm text-zinc-100 mb-3"
            >
              <option value="">Select target speaker</option>
              {mergeCandidates.map(s => (
                <option key={s.id} value={s.id}>{s.display_name}</option>
              ))}
            </select>

            {detail && selectedMergeTarget && (
              <div className="mb-3 rounded border border-zinc-800 bg-zinc-950/60 p-3 text-xs text-zinc-400">
                {detail.display_name} will be deleted after moving into {selectedMergeTarget.display_name}.
              </div>
            )}

            {detail && (
              <>
                <label className="block text-xs text-zinc-400 mb-1" htmlFor="merge-confirm-speaker">
                  Type “{detail.display_name}” to confirm
                </label>
                <input
                  id="merge-confirm-speaker"
                  value={mergeConfirm}
                  onChange={e => setMergeConfirm(e.target.value)}
                  className="w-full px-3 py-2 rounded bg-zinc-950 border border-zinc-700 text-sm text-zinc-100"
                />
              </>
            )}
          </div>
          <Dialog.Footer>
            <button
              onClick={() => setMergeOpen(false)}
              className="text-xs px-3 py-1.5 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800"
            >
              Cancel
            </button>
            <button
              onClick={mergeSelectedSpeaker}
              disabled={!mergeTargetId || !mergeConfirmMatches || merging}
              className="text-xs px-3 py-1.5 rounded bg-rose-700 hover:bg-rose-600 text-white disabled:opacity-50"
            >
              {merging ? 'Merging…' : 'Merge and delete source'}
            </button>
          </Dialog.Footer>
        </Dialog.Content>
      </Dialog>

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <Dialog.Content size="md" describedById="new-speaker-description">
          <Dialog.Header>
            <Dialog.Title>New speaker</Dialog.Title>
            <Dialog.Close />
          </Dialog.Header>
          <div className="px-5 py-4">
            <p id="new-speaker-description" className="sr-only">
              Create a new speaker by providing a display name and
              optional email + notes.
            </p>
            <label className="block text-xs text-zinc-400 mb-1">Display name *</label>
            <input
              value={newName}
              onChange={e => setNewName(e.target.value)}
              placeholder="e.g. Aaron Stransky"
              className="w-full px-3 py-2 rounded bg-zinc-950 border border-zinc-700 text-sm text-zinc-100 mb-3"
            />
            <label className="block text-xs text-zinc-400 mb-1">Email (optional)</label>
            <input
              value={newEmail}
              onChange={e => setNewEmail(e.target.value)}
              placeholder="aaron@magicunicorn.tech"
              className="w-full px-3 py-2 rounded bg-zinc-950 border border-zinc-700 text-sm text-zinc-100 mb-3"
            />
            <label className="block text-xs text-zinc-400 mb-1">Notes (optional)</label>
            <textarea
              value={newNotes}
              onChange={e => setNewNotes(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 rounded bg-zinc-950 border border-zinc-700 text-sm text-zinc-100"
            />
          </div>
          <Dialog.Footer>
            <button
              onClick={() => setShowCreate(false)}
              className="text-xs px-3 py-1.5 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800"
            >
              Cancel
            </button>
            <button
              onClick={createSpeaker}
              disabled={creating || !newName.trim()}
              className="text-xs px-3 py-1.5 rounded bg-fuchsia-600 hover:bg-fuchsia-500 text-white disabled:opacity-50"
            >
              {creating ? 'Creating…' : 'Create speaker'}
            </button>
          </Dialog.Footer>
        </Dialog.Content>
      </Dialog>

      {/* v3.19 (audit §6). Replaces `window.confirm()` on the sample +
          speaker delete flows. ConfirmModal defaults focus to Cancel
          for destructive tone, so an accidental Enter doesn't delete. */}
      <ConfirmModal
        isOpen={pendingDeleteSampleId !== null}
        title="Delete voice sample?"
        description="Delete this voice sample? Centroid will be recomputed from the remaining samples."
        confirmLabel="Delete sample"
        cancelLabel="Cancel"
        tone="danger"
        onConfirm={() => {
          const sampleId = pendingDeleteSampleId;
          setPendingDeleteSampleId(null);
          if (sampleId !== null) {
            void deleteSample(sampleId);
          }
        }}
        onCancel={() => setPendingDeleteSampleId(null)}
      />
      <ConfirmModal
        isOpen={pendingDeleteSpeakerId !== null}
        title="Delete speaker?"
        description="Delete this speaker? Existing meeting links will be cleared. This cannot be undone."
        confirmLabel="Delete speaker"
        cancelLabel="Cancel"
        tone="danger"
        onConfirm={() => {
          const speakerId = pendingDeleteSpeakerId;
          setPendingDeleteSpeakerId(null);
          if (speakerId !== null) {
            void deleteSpeaker(speakerId);
          }
        }}
        onCancel={() => setPendingDeleteSpeakerId(null)}
      />
    </div>
  );
};
