import React, { useMemo, useRef, useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft, MoreVertical, X, Check, Edit2, RefreshCw, Mail, Share2,
  FileText, FileAudio, Brain, Users, Sparkles, Calendar, Clock, Zap, Loader2,
  Trash2, Tag as TagIcon, Search, ChevronDown, Mic, Link2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import AudioPlayer from './AudioPlayer';
import { SpeakerLabelEditor } from './SpeakerLabelEditor';
import { SpeakerTimeline } from './SpeakerTimeline';
import { TagChip } from './TagChip';
import { FederationSummaryApproval } from './FederationSummaryApproval';
import { Briefcase, AlertTriangle } from 'lucide-react';
import { config } from '../config';
import { formatLifecycleDate, formatLifecycleTimestamp } from '../utils/lifecycleTimestamp';

type TabKey =
  | 'summary'
  | 'action_items'
  | 'transcript'
  | 'speakers'
  | 'insights'
  | 'people';

interface TranscriptionSegment {
  start: number;
  end: number;
  text: string;
  speaker?: string;
  confidence?: number;
}

interface Participant {
  id: string;
  name: string;
  email?: string | null;
  role?: string | null;
  contact_id?: string | null;
  contact_match_confidence?: number | null;
  contact_match_basis?: string | null;
  contact_link_source?: string | null;
}

interface ContactOpsPerson {
  person_id: string;
  display_name: string;
  email?: string | null;
  match_confidence: number;
  match_basis: string;
}

interface ActionItem {
  id: number;
  text: string;
  owner: string | null;
  due_date: string | null;
  status: string;
  sort_order: number;
  source: string;
  project_ops_link_state: 'local_only' | 'proposed' | 'approved_linked' | 'rejected' | 'sync_failed';
  project_ops_task_url?: string | null;
  project_ops_project_number?: string | null;
  project_ops_task_status?: string | null;
  project_ops_submitted_at?: string | null;
  project_ops_last_synced_at?: string | null;
  project_ops_sync_error?: string | null;
  project_ops_retry_count?: number;
}

interface SpeakerLinkRow {
  id: number;
  raw_label: string;
  speaker_id: number | null;
  speaker_display?: string | null;
}

interface OrgSpeaker {
  id: number;
  display_name: string;
  has_centroid: boolean;
}

/** Another user's copy of the same meeting (duplicate detection, v3.36).
 *  Mirrors RelatedSession in pages/SessionDetails.tsx — same data contract. */
interface RelatedSession {
  id: number;
  name: string;
  recorded_by?: string | null;
  started_at?: string | null;
  duration?: number | null;
}

interface Props {
  // Session + derived
  session: any;
  sessionId?: string;
  summaryApprovalHeaders: HeadersInit;
  summaryApprovalVersion: string;
  segments: TranscriptionSegment[];
  speakers: string[];
  transcriptText: string;
  hasTranscript: boolean;

  // Filters
  selectedSpeaker: string;
  setSelectedSpeaker: (s: string) => void;
  searchTerm: string;
  setSearchTerm: (s: string) => void;

  // Audio
  audioRef: React.RefObject<HTMLAudioElement | null>;
  audioSrc: string | null;
  currentTime: number;
  setCurrentTime: (t: number) => void;

  // Speakers
  speakerLinks: SpeakerLinkRow[];
  orgSpeakers: OrgSpeaker[];
  onSpeakerChanged: () => void;

  // Insights
  insights: any | null;
  insightsLoading: boolean;

  // Title
  editingTitle: boolean;
  titleDraft: string;
  savingTitle: boolean;
  startEditTitle: () => void;
  setTitleDraft: (s: string) => void;
  setEditingTitle: (b: boolean) => void;
  saveTitle: () => void;

  // Participants
  participants: Participant[];
  newParticipantName: string;
  setNewParticipantName: (s: string) => void;
  newParticipantEmail: string;
  setNewParticipantEmail: (s: string) => void;
  newParticipantRole: string;
  setNewParticipantRole: (s: string) => void;
  participantBusy: boolean;
  participantError: string | null;
  editingParticipantId: string | null;
  editParticipantName: string;
  setEditParticipantName: (s: string) => void;
  editParticipantEmail: string;
  setEditParticipantEmail: (s: string) => void;
  editParticipantRole: string;
  setEditParticipantRole: (s: string) => void;
  startEditParticipant: (p: Participant) => void;
  cancelEditParticipant: () => void;
  saveEditParticipant: () => void;
  addParticipant: () => void;
  deleteParticipant: (id: string) => void;
  contactSearchParticipantId: string | null;
  contactSearchQuery: string;
  setContactSearchQuery: (s: string) => void;
  contactSearchResults: ContactOpsPerson[];
  contactSearchLoading: boolean;
  contactSearchAmbiguous: boolean;
  openContactSearch: (participant: Participant) => void;
  closeContactSearch: () => void;
  stampParticipantContact: (participantId: string, person: ContactOpsPerson | null) => void;

  // Action items (first-class table, see api/action_items.py)
  actionItems: ActionItem[];
  actionUpdatingIds: Set<number>;
  newActionText: string;
  setNewActionText: (s: string) => void;
  newActionOwner: string;
  setNewActionOwner: (s: string) => void;
  actionBusy: boolean;
  actionError: string | null;
  setActionStatus: (id: number, status: string) => Promise<void> | void;
  requeueProjectOpsAction: (id: number) => Promise<void> | void;
  actionItemsOpen: boolean;
  targetActionItemId: number | null;
  onOpenActionItems: () => Promise<void> | void;
  addActionItem: () => void;
  deleteActionItem: (id: number) => void;

  // Tags
  tags: string[];
  newTag: string;
  setNewTag: (s: string) => void;
  tagBusy: boolean;
  tagError: string | null;
  addTag: () => void;
  removeTag: (tag: string) => void;
  setTagError: (s: string | null) => void;

  // Top-level actions
  onBack: () => void;
  onOpenShare: () => void;
  onOpenEmail: () => void;
  onOpenReprocess: () => void;
  onUploadAudio: () => void;
  uploading: boolean;

  // Misc
  formatTime: (s: number) => string;
  formatDate: (s: string) => string;
}

const TAB_CONFIG: Array<{ key: TabKey; label: string; icon: React.ComponentType<any> }> = [
  { key: 'summary', label: 'Summary', icon: Sparkles },
  { key: 'action_items', label: 'Action items', icon: Zap },
  { key: 'transcript', label: 'Transcript', icon: FileText },
  { key: 'speakers', label: 'Speakers', icon: Users },
  { key: 'insights', label: 'Insights', icon: Brain },
  { key: 'people', label: 'People & Tags', icon: TagIcon },
];

const PALETTE = [
  'bg-purple-500', 'bg-blue-500', 'bg-emerald-500',
  'bg-amber-500', 'bg-rose-500', 'bg-indigo-500',
  'bg-teal-500', 'bg-orange-500',
];

export default function MobileSessionDetails(props: Props) {
  const {
    session, sessionId, summaryApprovalHeaders, summaryApprovalVersion, segments, speakers, transcriptText, hasTranscript,
    selectedSpeaker, setSelectedSpeaker, searchTerm, setSearchTerm,
    audioRef, audioSrc, currentTime, setCurrentTime,
    speakerLinks, orgSpeakers, onSpeakerChanged,
    insights, insightsLoading,
    editingTitle, titleDraft, savingTitle, startEditTitle, setTitleDraft, setEditingTitle, saveTitle,
    participants,
    newParticipantName, setNewParticipantName,
    newParticipantEmail, setNewParticipantEmail,
    newParticipantRole, setNewParticipantRole,
    participantBusy, participantError,
    editingParticipantId,
    editParticipantName, setEditParticipantName,
    editParticipantEmail, setEditParticipantEmail,
    editParticipantRole, setEditParticipantRole,
    startEditParticipant, cancelEditParticipant, saveEditParticipant,
    addParticipant, deleteParticipant,
    contactSearchParticipantId, contactSearchQuery, setContactSearchQuery,
    contactSearchResults, contactSearchLoading, contactSearchAmbiguous,
    openContactSearch, closeContactSearch, stampParticipantContact,
    actionItems,
    actionUpdatingIds,
    newActionText, setNewActionText,
    newActionOwner, setNewActionOwner,
    actionBusy, actionError,
    setActionStatus,
    requeueProjectOpsAction,
    actionItemsOpen,
    targetActionItemId,
    onOpenActionItems,
    addActionItem,
    deleteActionItem,
    tags, newTag, setNewTag, tagBusy, tagError, addTag, removeTag, setTagError,
    onBack, onOpenShare, onOpenEmail, onOpenReprocess, onUploadAudio, uploading,
    formatTime, formatDate,
  } = props;

  const [tab, setTab] = useState<TabKey>(
    actionItemsOpen || targetActionItemId !== null ? 'action_items' : 'summary',
  );
  const [moreOpen, setMoreOpen] = useState(false);
  const segmentRefs = useRef<Array<HTMLDivElement | null>>([]);
  const navigate = useNavigate();

  // v3.36 duplicate detection: dismissible "{recorded_by} also recorded
  // this meeting" banner, ported from the desktop page. Component-state
  // only (no persistence); reset when the user navigates to a different
  // session so each copy gets its own banner.
  const relatedSessions: RelatedSession[] = Array.isArray(session?.related_sessions)
    ? session.related_sessions
    : [];
  const [relatedBannerDismissed, setRelatedBannerDismissed] = useState(false);
  const sessionKey = String(session?.id ?? sessionId ?? '');
  useEffect(() => {
    setRelatedBannerDismissed(false);
  }, [sessionKey]);

  useEffect(() => {
    if (actionItemsOpen || targetActionItemId !== null) {
      setTab('action_items');
    }
  }, [actionItemsOpen, targetActionItemId]);

  useEffect(() => {
    if (tab === 'action_items') {
      void onOpenActionItems();
    }
  }, [onOpenActionItems, tab]);

  const filteredSegments = useMemo(() => {
    return segments.filter((seg) => {
      const speakerMatch = selectedSpeaker === 'all' || seg.speaker === selectedSpeaker;
      const searchMatch = !searchTerm || seg.text.toLowerCase().includes(searchTerm.toLowerCase());
      return speakerMatch && searchMatch;
    });
  }, [segments, selectedSpeaker, searchTerm]);

  const activeSegmentIndex = useMemo(() => {
    return filteredSegments.findIndex((s) => currentTime >= s.start && currentTime <= s.end);
  }, [filteredSegments, currentTime]);

  useEffect(() => {
    if (tab !== 'transcript') return;
    if (activeSegmentIndex < 0) return;
    const el = segmentRefs.current[activeSegmentIndex];
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [activeSegmentIndex, tab]);

  const seekTo = (t: number) => {
    if (!audioRef.current) return;
    audioRef.current.currentTime = t;
    audioRef.current.play().catch(() => undefined);
  };

  const summarySrc = session?.final_summary || session?.summary?.analysis || session?.summary || null;
  const summaryExec: string = summarySrc?.executive
    || insights?.summary
    || session?.summary?.executive
    || '';
  const summaryBullets: string[] = summarySrc?.bullets || [];
  const summaryActions: any[] = summarySrc?.actions || insights?.action_items || [];
  const summaryDecisions: any[] = summarySrc?.decisions || insights?.key_decisions || [];
  const hasAnySummary = Boolean(summaryExec || summaryBullets.length || summaryActions.length || summaryDecisions.length);

  const speakerMetrics = useMemo(() => {
    if (!segments.length) return [];
    const tally: Record<string, { seconds: number; words: number }> = {};
    segments.forEach((seg) => {
      const spk = seg.speaker || 'Unknown';
      if (!tally[spk]) tally[spk] = { seconds: 0, words: 0 };
      tally[spk].seconds += Math.max(0, (seg.end || 0) - (seg.start || 0));
      tally[spk].words += (seg.text || '').trim().split(/\s+/).filter(Boolean).length;
    });
    const total = Object.values(tally).reduce((a, b) => a + b.seconds, 0) || 1;
    return Object.entries(tally)
      .map(([speaker, v]) => ({ speaker, seconds: v.seconds, words: v.words, pct: (v.seconds / total) * 100 }))
      .sort((a, b) => b.seconds - a.seconds);
  }, [segments]);

  const timelineDuration = session?.duration
    || segments.reduce((m, s) => Math.max(m, s.end || 0), 0);

  return (
    <div
      className="flex flex-col bg-zinc-950 text-zinc-100 min-h-screen md:hidden"
      style={{
        paddingTop: 'env(safe-area-inset-top)',
        paddingLeft: 'env(safe-area-inset-left)',
        paddingRight: 'env(safe-area-inset-right)',
      }}
    >
      <div className="sticky top-0 z-20 bg-zinc-950/95 backdrop-blur border-b border-zinc-800">
        <div className="flex items-center gap-2 px-3 pt-3 pb-2">
          <button
            type="button"
            onClick={onBack}
            className="w-9 h-9 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-200 active:bg-zinc-800"
            aria-label="Back to sessions"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div className="flex-1 min-w-0">
            {editingTitle ? (
              <div className="flex items-center gap-1.5">
                <input
                  value={titleDraft}
                  onChange={(e) => setTitleDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') saveTitle();
                    if (e.key === 'Escape') setEditingTitle(false);
                  }}
                  autoFocus
                  maxLength={200}
                  className="flex-1 bg-transparent border-b border-fuchsia-500 text-base font-semibold text-white outline-none py-1"
                />
                <button
                  type="button"
                  onClick={saveTitle}
                  disabled={savingTitle}
                  className="w-8 h-8 rounded-lg bg-fuchsia-600 flex items-center justify-center text-white disabled:opacity-50"
                  aria-label="Save title"
                >
                  {savingTitle ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                </button>
                <button
                  type="button"
                  onClick={() => setEditingTitle(false)}
                  disabled={savingTitle}
                  className="w-8 h-8 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300"
                  aria-label="Cancel"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={startEditTitle}
                className="flex items-center gap-1.5 w-full text-left"
                title="Tap to rename"
              >
                <span className="truncate text-base font-semibold text-white">
                  {session?.title || session?.name || 'Meeting Session'}
                </span>
                <Edit2 className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
              </button>
            )}
            <div className="flex items-center gap-3 mt-0.5 text-[11px] text-zinc-500">
              {session?.created_at && (
                <span className="flex items-center gap-1">
                  <Calendar className="w-3 h-3" />
                  {formatDate(session.created_at)}
                </span>
              )}
              {session?.duration ? (
                <span className="flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {formatTime(session.duration)}
                </span>
              ) : null}
              {speakers.length > 0 && (
                <span className="flex items-center gap-1">
                  <Users className="w-3 h-3" />
                  {speakers.length}
                </span>
              )}
              {session?.project_app && session?.project_slug && (
                <span
                  className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full border ${
                    session.project_app === 'crisis-ops'
                      ? 'border-red-800/60 bg-red-900/20 text-red-300'
                      : 'border-purple-800/60 bg-purple-900/20 text-purple-300'
                  }`}
                  title={`Linked to ${session.project_app} / ${session.project_slug}`}
                >
                  {session.project_app === 'crisis-ops'
                    ? <AlertTriangle className="w-3 h-3" />
                    : <Briefcase className="w-3 h-3" />}
                  <span className="truncate max-w-[12ch]">{session.project_slug}</span>
                </span>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={() => setMoreOpen(true)}
            className="w-9 h-9 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-200 active:bg-zinc-800"
            aria-label="More actions"
          >
            <MoreVertical className="w-4 h-4" />
          </button>
        </div>

        <div className="overflow-x-auto scrollbar-none">
          <div className="flex items-center gap-1.5 px-3 pb-2 min-w-max">
            {TAB_CONFIG.map((t) => {
              const Icon = t.icon;
              const active = tab === t.key;
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setTab(t.key)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
                    active
                      ? 'bg-gradient-to-r from-fuchsia-600 to-indigo-600 text-white shadow-sm shadow-fuchsia-900/40'
                      : 'bg-zinc-900 border border-zinc-800 text-zinc-300 active:bg-zinc-800'
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" />
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div
        className="flex-1 min-h-0 overflow-y-auto px-3 pt-3"
        style={{ paddingBottom: audioSrc ? 'calc(env(safe-area-inset-bottom) + 96px)' : 'calc(env(safe-area-inset-bottom) + 16px)' }}
      >
        {/* v3.36 duplicate detection: another org member also recorded this
            same meeting. Dismissible info banner (dismiss is component-state
            only — no persistence) with a link to each related copy's detail
            page. Rendered above the tab content so it shows on every tab. */}
        {!relatedBannerDismissed && relatedSessions.length > 0 && (
          <div className="rounded-2xl bg-amber-500/10 border border-amber-500/30 p-3.5 mb-3 flex items-start gap-2.5">
            <Mic className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-amber-200">
                This meeting may have been recorded more than once
              </h3>
              <ul className="mt-1 space-y-1.5">
                {relatedSessions.map((related) => (
                  <li key={related.id} className="text-xs text-amber-200/90 leading-relaxed">
                    <span>
                      It looks like {related.recorded_by || 'someone else'} also recorded this meeting.
                    </span>{' '}
                    <button
                      type="button"
                      onClick={() => navigate(`/sessions/${related.id}`)}
                      className="font-medium text-amber-300 underline underline-offset-2 active:text-amber-100"
                      title={related.name ? `Open "${related.name}"` : 'Open their copy'}
                    >
                      View their copy
                    </button>
                  </li>
                ))}
              </ul>
            </div>
            <button
              type="button"
              onClick={() => setRelatedBannerDismissed(true)}
              className="shrink-0 p-1 rounded text-amber-400/80 active:text-amber-100 active:bg-amber-500/20"
              title="Dismiss"
              aria-label="Dismiss duplicate-recording notice"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        )}

        {tab === 'summary' && (
          <SummaryTab
            insightsLoading={insightsLoading}
            summaryExec={summaryExec}
            summaryBullets={summaryBullets}
            summaryActions={summaryActions}
            summaryDecisions={summaryDecisions}
            hasAnySummary={hasAnySummary}
            status={session?.status}
            sessionIdStr={String(session?.id || sessionId || '')}
            summaryApprovalHeaders={summaryApprovalHeaders}
            summaryApprovalVersion={summaryApprovalVersion}
            actionItems={actionItems}
            actionUpdatingIds={actionUpdatingIds}
            newActionText={newActionText}
            setNewActionText={setNewActionText}
            newActionOwner={newActionOwner}
            setNewActionOwner={setNewActionOwner}
            actionBusy={actionBusy}
            actionError={actionError}
            setActionStatus={setActionStatus}
            requeueProjectOpsAction={requeueProjectOpsAction}
            addActionItem={addActionItem}
            deleteActionItem={deleteActionItem}
            actionItemsOnly={false}
            targetActionItemId={targetActionItemId}
          />
        )}

        {tab === 'action_items' && (
          <SummaryTab
            insightsLoading={insightsLoading}
            summaryExec={summaryExec}
            summaryBullets={summaryBullets}
            summaryActions={summaryActions}
            summaryDecisions={summaryDecisions}
            hasAnySummary={hasAnySummary}
            status={session?.status}
            sessionIdStr={String(session?.id || sessionId || '')}
            summaryApprovalHeaders={summaryApprovalHeaders}
            summaryApprovalVersion={summaryApprovalVersion}
            actionItems={actionItems}
            actionUpdatingIds={actionUpdatingIds}
            newActionText={newActionText}
            setNewActionText={setNewActionText}
            newActionOwner={newActionOwner}
            setNewActionOwner={setNewActionOwner}
            actionBusy={actionBusy}
            actionError={actionError}
            setActionStatus={setActionStatus}
            requeueProjectOpsAction={requeueProjectOpsAction}
            addActionItem={addActionItem}
            deleteActionItem={deleteActionItem}
            actionItemsOnly
            targetActionItemId={targetActionItemId}
          />
        )}

        {tab === 'transcript' && (
          <TranscriptTab
            segments={segments}
            filteredSegments={filteredSegments}
            speakers={speakers}
            selectedSpeaker={selectedSpeaker}
            setSelectedSpeaker={setSelectedSpeaker}
            searchTerm={searchTerm}
            setSearchTerm={setSearchTerm}
            transcriptText={transcriptText}
            hasTranscript={hasTranscript}
            status={session?.status}
            reprocessStatus={session?.metadata?.reprocess_status}
            activeSegmentIndex={activeSegmentIndex}
            segmentRefs={segmentRefs}
            seekTo={seekTo}
            speakerLinks={speakerLinks}
            orgSpeakers={orgSpeakers}
            onSpeakerChanged={onSpeakerChanged}
            sessionIdStr={String(session?.id || sessionId || '')}
            formatTime={formatTime}
          />
        )}

        {tab === 'speakers' && (
          <SpeakersTab
            speakers={speakers}
            speakerMetrics={speakerMetrics}
            timelineDuration={timelineDuration}
            segments={segments}
            currentTime={currentTime}
            seekTo={seekTo}
            speakerLinks={speakerLinks}
            orgSpeakers={orgSpeakers}
            onSpeakerChanged={onSpeakerChanged}
            sessionIdStr={String(session?.id || sessionId || '')}
          />
        )}

        {tab === 'insights' && (
          <InsightsTab
            insights={insights}
            insightsLoading={insightsLoading}
          />
        )}

        {tab === 'people' && (
          <PeopleTagsTab
            participants={participants}
            newParticipantName={newParticipantName} setNewParticipantName={setNewParticipantName}
            newParticipantEmail={newParticipantEmail} setNewParticipantEmail={setNewParticipantEmail}
            newParticipantRole={newParticipantRole} setNewParticipantRole={setNewParticipantRole}
            participantBusy={participantBusy} participantError={participantError}
            editingParticipantId={editingParticipantId}
            editParticipantName={editParticipantName} setEditParticipantName={setEditParticipantName}
            editParticipantEmail={editParticipantEmail} setEditParticipantEmail={setEditParticipantEmail}
            editParticipantRole={editParticipantRole} setEditParticipantRole={setEditParticipantRole}
            startEditParticipant={startEditParticipant} cancelEditParticipant={cancelEditParticipant}
            saveEditParticipant={saveEditParticipant} addParticipant={addParticipant}
            deleteParticipant={deleteParticipant}
            contactSearchParticipantId={contactSearchParticipantId}
            contactSearchQuery={contactSearchQuery}
            setContactSearchQuery={setContactSearchQuery}
            contactSearchResults={contactSearchResults}
            contactSearchLoading={contactSearchLoading}
            contactSearchAmbiguous={contactSearchAmbiguous}
            openContactSearch={openContactSearch}
            closeContactSearch={closeContactSearch}
            stampParticipantContact={stampParticipantContact}
            tags={tags} newTag={newTag} setNewTag={setNewTag} tagBusy={tagBusy} tagError={tagError}
            addTag={addTag} removeTag={removeTag} setTagError={setTagError}
          />
        )}
      </div>

      {audioSrc && (
        <div
          className="fixed bottom-0 left-0 right-0 z-30 bg-zinc-950/95 backdrop-blur border-t border-zinc-800 px-3 pt-2"
          style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 6px)' }}
        >
          <AudioPlayer
            src={audioSrc}
            downloadAs={`${(session?.title || session?.name || 'meeting').replace(/\s+/g, '_')}.wav`}
            externalAudioRef={audioRef as any}
            onTimeUpdate={(t) => setCurrentTime(t)}
            className="!bg-zinc-900 !border-zinc-800 !p-2"
          />
        </div>
      )}

      {moreOpen && (
        <div className="fixed inset-0 z-40 flex flex-col">
          <button
            type="button"
            className="flex-1 bg-black/60"
            onClick={() => setMoreOpen(false)}
            aria-label="Close menu"
          />
          <div
            className="bg-zinc-950 border-t border-zinc-800 rounded-t-3xl px-5 pt-4"
            style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 16px)' }}
          >
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-white font-semibold text-base">Session actions</h2>
              <button
                type="button"
                onClick={() => setMoreOpen(false)}
                className="w-9 h-9 rounded-full bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="space-y-2">
              <MoreItem
                icon={Share2}
                label="Share meeting"
                hint="Invite teammates or guests"
                onClick={() => { setMoreOpen(false); onOpenShare(); }}
              />
              <MoreItem
                icon={Mail}
                label="Email attendees"
                hint="Send summary, transcript, or audio"
                onClick={() => { setMoreOpen(false); onOpenEmail(); }}
              />
              {session?.audio_file && (
                <MoreItem
                  icon={RefreshCw}
                  label="Re-process recording"
                  hint="Re-run transcribe / diarize / summarize"
                  onClick={() => { setMoreOpen(false); onOpenReprocess(); }}
                />
              )}
              {!session?.audio_file && (
                <MoreItem
                  icon={Sparkles}
                  label={uploading ? 'Uploading…' : 'Upload audio'}
                  hint="Attach a recording to this session"
                  onClick={() => { setMoreOpen(false); onUploadAudio(); }}
                  disabled={uploading}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function MoreItem({
  icon: Icon, label, hint, onClick, disabled,
}: {
  icon: React.ComponentType<any>;
  label: string;
  hint?: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="w-full flex items-center gap-3 px-3 py-3 rounded-xl bg-zinc-900 border border-zinc-800 active:bg-zinc-800 text-left disabled:opacity-50"
    >
      <span className="w-9 h-9 rounded-lg bg-gradient-to-br from-fuchsia-500/20 to-indigo-500/20 border border-fuchsia-500/30 flex items-center justify-center text-fuchsia-300">
        <Icon className="w-4 h-4" />
      </span>
      <span className="flex-1 min-w-0">
        <span className="block text-sm font-medium text-zinc-100">{label}</span>
        {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
      </span>
    </button>
  );
}

function SectionCard({ title, icon: Icon, children, className }: {
  title: string;
  icon: React.ComponentType<any>;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`rounded-2xl bg-zinc-900/60 border border-zinc-800 p-4 ${className || ''}`}>
      <div className="flex items-center gap-2 mb-3">
        <Icon className="w-4 h-4 text-fuchsia-400" />
        <h3 className="text-sm font-semibold text-zinc-100">{title}</h3>
      </div>
      {children}
    </div>
  );
}

function SummaryTab({
  insightsLoading, summaryExec, summaryBullets, summaryActions, summaryDecisions, hasAnySummary, status,
  sessionIdStr, summaryApprovalHeaders, summaryApprovalVersion,
  actionItems,
  actionUpdatingIds,
  newActionText, setNewActionText,
  newActionOwner, setNewActionOwner,
  actionBusy, actionError,
  setActionStatus,
  requeueProjectOpsAction,
  addActionItem,
  deleteActionItem,
  actionItemsOnly,
  targetActionItemId,
}: {
  insightsLoading: boolean;
  summaryExec: string;
  summaryBullets: string[];
  summaryActions: any[];
  summaryDecisions: any[];
  hasAnySummary: boolean;
  status?: string;
  sessionIdStr: string;
  summaryApprovalHeaders: HeadersInit;
  summaryApprovalVersion: string;
  actionItems: ActionItem[];
  actionUpdatingIds: Set<number>;
  newActionText: string;
  setNewActionText: (s: string) => void;
  newActionOwner: string;
  setNewActionOwner: (s: string) => void;
  actionBusy: boolean;
  actionError: string | null;
  setActionStatus: (id: number, status: string) => Promise<void> | void;
  requeueProjectOpsAction: (id: number) => Promise<void> | void;
  addActionItem: () => void;
  deleteActionItem: (id: number) => void;
  actionItemsOnly: boolean;
  targetActionItemId: number | null;
}) {
  const [expandedActionId, setExpandedActionId] = useState<number | null>(
    targetActionItemId,
  );
  const hasPersistedActions = Array.isArray(actionItems) && actionItems.length > 0;
  useEffect(() => {
    if (targetActionItemId === null) return;
    setExpandedActionId(targetActionItemId);
    window.requestAnimationFrame(() => {
      document
        .getElementById(`mobile-action-item-${targetActionItemId}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }, [actionItems, targetActionItemId]);
  const approvalControl = (
    <FederationSummaryApproval
      apiUrl={config.apiUrl}
      sessionId={sessionIdStr}
      headers={summaryApprovalHeaders}
      summaryVersion={summaryApprovalVersion}
    />
  );
  // hasAnySummary includes the empty-state case for the LLM summary
  // sections. Action items live in their own table so we want to show
  // the panel even if the summary text hasn't generated yet (e.g. the
  // user is manually adding items first).
  if (!actionItemsOnly && !hasAnySummary && !hasPersistedActions && insightsLoading) {
    return (
      <div className="space-y-3">
        {approvalControl}
        <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 flex items-center justify-center gap-3">
          <Loader2 className="w-4 h-4 text-fuchsia-400 animate-spin" />
          <span className="text-sm text-zinc-400">Loading summary…</span>
        </div>
      </div>
    );
  }

  if (!actionItemsOnly && !hasAnySummary && !hasPersistedActions) {
    return (
      <div className="space-y-3">
        {approvalControl}
        <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 text-sm text-zinc-400">
          {status === 'processing'
            ? 'Generating summary… check back in a moment.'
            : 'No summary available yet. Re-process this recording to generate one.'}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {!actionItemsOnly && approvalControl}
      {!actionItemsOnly && summaryExec && (
        <SectionCard title="Summary" icon={Sparkles}>
          <div className="prose prose-invert prose-sm max-w-none text-zinc-200 leading-relaxed text-sm session-md">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{String(summaryExec)}</ReactMarkdown>
          </div>
        </SectionCard>
      )}

      {!actionItemsOnly && summaryBullets.length > 0 && (
        <SectionCard title="Key discussion points" icon={FileText}>
          <ul className="space-y-2">
            {summaryBullets.map((b, i) => (
              <li key={i} className="flex items-start gap-2 text-sm text-zinc-200">
                <span className="text-fuchsia-400 mt-1.5">•</span>
                <span>{b}</span>
              </li>
            ))}
          </ul>
        </SectionCard>
      )}

      {(hasPersistedActions || true) && (
        <SectionCard title="Action items" icon={Zap}>
          {actionError && (
            <div className="mb-2 rounded border border-red-500/30 bg-red-500/10 px-2 py-1 text-xs text-red-200">
              {actionError}
            </div>
          )}
          {hasPersistedActions ? (
            <div className="space-y-2">
              {actionItems.map((item) => {
                const busy = actionUpdatingIds.has(item.id);
                const done = item.status === 'done';
                const cancelled = item.status === 'cancelled';
                const poState = item.project_ops_link_state || 'local_only';
                const hasProjectOpsHistory =
                  poState === 'proposed' ||
                  poState === 'approved_linked' ||
                  (poState === 'sync_failed' &&
                    Boolean(item.project_ops_submitted_at));
                return (
                  <div
                    key={item.id}
                    id={`mobile-action-item-${item.id}`}
                    className={`rounded-lg border px-3 py-2 ${done ? 'border-emerald-500/30 bg-emerald-500/10' : cancelled ? 'border-zinc-700 bg-zinc-800/40' : 'border-amber-500/30 bg-amber-500/10'}`}
                  >
                    <div className="flex items-start gap-2">
                      <button
                        type="button"
                        onClick={() => setActionStatus(item.id, done ? 'todo' : 'done')}
                        disabled={busy}
                        className="mt-0.5 shrink-0 disabled:opacity-50"
                        aria-label={done ? 'Mark not done' : 'Mark done'}
                      >
                        {done ? (
                          <Check className="h-4 w-4 text-emerald-400" />
                        ) : (
                          <span className="inline-block h-4 w-4 rounded border-2 border-amber-400" />
                        )}
                      </button>
                      <div className="min-w-0 flex-1">
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedActionId((current) =>
                              current === item.id ? null : item.id
                            )
                          }
                          className={`text-sm font-medium ${done ? 'line-through text-zinc-400' : cancelled ? 'text-zinc-400' : 'text-zinc-100'}`}
                          aria-expanded={expandedActionId === item.id}
                        >
                          {item.text}
                        </button>
                        {(item.owner || item.due_date || item.source === 'manual') && (
                          <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-zinc-400">
                            {item.owner && <span>Owner: {item.owner}</span>}
                            {formatLifecycleDate(item.due_date) && (
                              <span>Due {formatLifecycleDate(item.due_date)}</span>
                            )}
                            {item.source === 'manual' && (
                              <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-zinc-300">manual</span>
                            )}
                          </div>
                        )}
                        <div className="mt-1 text-[11px]">
                          {poState === 'proposed' && (
                            <span className="text-fuchsia-300">Project-Ops proposed</span>
                          )}
                          {poState === 'approved_linked' && (
                            <span className="text-indigo-300">Project-Ops linked</span>
                          )}
                          {poState === 'rejected' && (
                            <span className="text-zinc-400">Project-Ops rejected</span>
                          )}
                          {poState === 'sync_failed' && (
                            <span className="text-red-300">Project-Ops sync failed</span>
                          )}
                        </div>
                        {expandedActionId === item.id && (
                          <div className="mt-2 rounded border border-zinc-700 bg-zinc-950/50 p-2 text-xs text-zinc-300">
                            <p>
                              {poState === 'approved_linked'
                                ? `Project-Ops task status (read-only): ${
                                    item.project_ops_task_status || 'unknown'
                                  }.`
                                : poState === 'proposed'
                                  ? 'Awaiting human review in Project-Ops.'
                                  : poState === 'rejected'
                                    ? 'Rejected in Project-Ops; no task was created.'
                                    : poState === 'sync_failed'
                                      ? `Sync failed${
                                          item.project_ops_sync_error
                                            ? ` (${item.project_ops_sync_error})`
                                            : ''
                                        }.`
                                      : 'Local to Meeting-Ops. The checkbox changes only the local status.'}
                            </p>
                            {formatLifecycleTimestamp(item.project_ops_last_synced_at) && (
                              <p className="mt-1 text-[11px] text-zinc-500">
                                Last successful sync{' '}
                                {formatLifecycleTimestamp(item.project_ops_last_synced_at)}
                                {item.project_ops_retry_count
                                  ? ` · ${item.project_ops_retry_count} failed attempt${
                                      item.project_ops_retry_count === 1 ? '' : 's'
                                    }`
                                  : ''}
                              </p>
                            )}
                            <div className="mt-2 flex flex-wrap gap-2">
                              {item.project_ops_task_url && (
                                <a
                                  href={item.project_ops_task_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="rounded bg-indigo-500 px-2 py-1 text-[11px] font-medium text-white"
                                >
                                  Open in Project-Ops
                                </a>
                              )}
                              {(poState === 'sync_failed' ||
                                poState === 'proposed' ||
                                poState === 'approved_linked') && (
                                <button
                                  type="button"
                                  onClick={() => requeueProjectOpsAction(item.id)}
                                  disabled={busy}
                                  className="inline-flex items-center gap-1 rounded border border-zinc-600 px-2 py-1 text-[11px]"
                                >
                                  <RefreshCw className={`h-3 w-3 ${busy ? 'animate-spin' : ''}`} />
                                  {poState === 'sync_failed' ? 'Retry sync' : 'Refresh status'}
                                </button>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                      <button
                        type="button"
                        onClick={() => deleteActionItem(item.id)}
                        disabled={busy || hasProjectOpsHistory}
                        className="shrink-0 rounded p-1 text-zinc-400 hover:bg-red-500/10 hover:text-red-300 disabled:opacity-50"
                        aria-label="Remove action item"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            // Legacy JSON-derived actions, kept as fallback for sessions
            // that haven't been re-summarized since the new table landed.
            summaryActions.length > 0 && (
              <div className="space-y-2">
                {summaryActions.map((a: any, i: number) => {
                  const text = typeof a === 'string' ? a : (a.action || a.text || '');
                  const owner = typeof a === 'object' ? a.owner : null;
                  return (
                    <div key={i} className="rounded-lg bg-amber-500/10 border border-amber-500/30 px-3 py-2">
                      <p className="text-sm text-zinc-100 font-medium">{text}</p>
                      {owner && (
                        <div className="mt-1 text-xs text-zinc-400">Owner: {owner}</div>
                      )}
                    </div>
                  );
                })}
              </div>
            )
          )}

          <div className="mt-3 space-y-2 border-t border-zinc-800 pt-2">
            <input
              type="text"
              value={newActionText}
              onChange={(e) => setNewActionText(e.target.value)}
              placeholder="Add an action item…"
              className="w-full rounded bg-zinc-900 border border-zinc-700 px-2 py-1.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
            />
            <input
              type="text"
              value={newActionOwner}
              onChange={(e) => setNewActionOwner(e.target.value)}
              placeholder="Owner (optional)"
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newActionText.trim() && !actionBusy) {
                  addActionItem();
                }
              }}
              className="w-full rounded bg-zinc-900 border border-zinc-700 px-2 py-1.5 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
            />
            <button
              type="button"
              onClick={addActionItem}
              disabled={actionBusy || !newActionText.trim()}
              className="w-full rounded bg-amber-500/80 hover:bg-amber-500 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              + Add action item
            </button>
          </div>
        </SectionCard>
      )}

      {!actionItemsOnly && summaryDecisions.length > 0 && (
        <SectionCard title="Key decisions" icon={Check}>
          <ul className="space-y-2">
            {summaryDecisions.map((d: any, i: number) => {
              const text = typeof d === 'string' ? d : (d.text || JSON.stringify(d));
              return (
                <li key={i} className="flex items-start gap-2 text-sm text-zinc-200">
                  <span className="text-emerald-400 mt-0.5">✓</span>
                  <span>{text}</span>
                </li>
              );
            })}
          </ul>
        </SectionCard>
      )}
    </div>
  );
}

function TranscriptTab({
  segments, filteredSegments, speakers, selectedSpeaker, setSelectedSpeaker, searchTerm, setSearchTerm,
  transcriptText, hasTranscript, status, reprocessStatus, activeSegmentIndex, segmentRefs, seekTo,
  speakerLinks, orgSpeakers, onSpeakerChanged, sessionIdStr, formatTime,
}: {
  segments: TranscriptionSegment[];
  filteredSegments: TranscriptionSegment[];
  speakers: string[];
  selectedSpeaker: string;
  setSelectedSpeaker: (s: string) => void;
  searchTerm: string;
  setSearchTerm: (s: string) => void;
  transcriptText: string;
  hasTranscript: boolean;
  status?: string;
  reprocessStatus?: string | null;
  activeSegmentIndex: number;
  segmentRefs: React.MutableRefObject<Array<HTMLDivElement | null>>;
  seekTo: (t: number) => void;
  speakerLinks: SpeakerLinkRow[];
  orgSpeakers: OrgSpeaker[];
  onSpeakerChanged: () => void;
  sessionIdStr: string;
  formatTime: (s: number) => string;
}) {
  // v3.55 honest empty-states — mobile parity with pages/SessionDetails.tsx.
  // A browser always-on session that never finalized (no audio ever reached
  // the server) stays at recording/active/failed with no segments, transcript,
  // or speakers — this tab used to show a bare "No transcript yet.", i.e. a
  // silent blank. reprocess_status is the server full-audio pipeline
  // (queued/in_progress while it runs; null for privacy-mode / never-uploaded).
  const isReprocessing =
    reprocessStatus === 'queued' || reprocessStatus === 'in_progress';
  const isProcessing = status === 'processing' || isReprocessing;
  const isEmptyRecord =
    speakers.length === 0 && segments.length === 0 && !transcriptText;
  // Never finalized: not completed, not processing, and nothing captured.
  const showUnfinalizedEmpty =
    isEmptyRecord && !isProcessing && status !== 'completed';

  return (
    <div className="space-y-3">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
        <input
          type="text"
          inputMode="search"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          placeholder="Search transcript"
          className="w-full pl-9 pr-3 py-2.5 rounded-xl bg-zinc-900 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
        />
      </div>

      {speakers.length > 1 && (
        <div className="overflow-x-auto scrollbar-none -mx-1 px-1">
          <div className="flex items-center gap-1.5 min-w-max py-1">
            <button
              type="button"
              onClick={() => setSelectedSpeaker('all')}
              className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                selectedSpeaker === 'all'
                  ? 'bg-purple-600 border-purple-500 text-white'
                  : 'bg-zinc-900 border-zinc-800 text-zinc-300'
              }`}
            >
              All ({segments.length})
            </button>
            {speakers.map((sp) => {
              const count = segments.filter((s) => s.speaker === sp).length;
              const active = selectedSpeaker === sp;
              return (
                <button
                  key={sp}
                  type="button"
                  onClick={() => setSelectedSpeaker(sp)}
                  className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                    active
                      ? 'bg-purple-600 border-purple-500 text-white'
                      : 'bg-zinc-900 border-zinc-800 text-zinc-300'
                  }`}
                >
                  {sp} ({count})
                </button>
              );
            })}
          </div>
        </div>
      )}

      {filteredSegments.length === 0 && transcriptText ? (
        <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-4 text-sm text-zinc-200 whitespace-pre-wrap leading-relaxed">
          {transcriptText}
        </div>
      ) : filteredSegments.length === 0 && isProcessing ? (
        <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-8 flex flex-col items-center gap-3">
          <Loader2 className="w-5 h-5 text-fuchsia-400 animate-spin" />
          <p className="text-sm text-zinc-400 text-center">Processing… transcript and speakers will appear when it finishes.</p>
        </div>
      ) : filteredSegments.length === 0 && !searchTerm && showUnfinalizedEmpty ? (
        <div className="rounded-2xl bg-amber-500/10 border border-amber-500/30 p-4 flex items-start gap-3">
          <FileAudio className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm font-semibold text-amber-200">This recording wasn&apos;t finalized</p>
            <p className="text-sm text-amber-200/80 mt-1">
              No audio reached the server, so there&apos;s no transcript or speakers to show.
              If you recorded on a phone, re-open the app on that device to check for a
              recovery prompt.
            </p>
          </div>
        </div>
      ) : filteredSegments.length === 0 ? (
        <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 text-sm text-zinc-400 text-center">
          {searchTerm
            ? 'No segments match your search.'
            : hasTranscript
              ? 'No segments to show for this filter.'
              : 'No transcript yet.'}
        </div>
      ) : (
        <div className="space-y-2">
          {filteredSegments.map((seg, idx) => {
            const isActive = idx === activeSegmentIndex;
            return (
              <div
                key={idx}
                ref={(el) => { segmentRefs.current[idx] = el; }}
                className={`rounded-xl border px-3 py-2.5 transition-colors ${
                  isActive
                    ? 'bg-fuchsia-500/10 border-fuchsia-500/40'
                    : 'bg-zinc-900/50 border-zinc-800 active:bg-zinc-900'
                }`}
              >
                <div className="flex items-center gap-2 mb-1.5">
                  <SpeakerLabelEditor
                    speaker={seg.speaker || 'Speaker'}
                    sessionId={sessionIdStr}
                    links={speakerLinks}
                    speakers={orgSpeakers}
                    onChanged={onSpeakerChanged}
                  />
                  <button
                    type="button"
                    onClick={() => seekTo(seg.start)}
                    className="text-[11px] text-zinc-400 tabular-nums underline-offset-2 hover:underline"
                  >
                    {formatTime(seg.start)}
                  </button>
                  {seg.confidence && (
                    <span className="text-[10px] text-zinc-600">
                      {Math.round(seg.confidence * 100)}%
                    </span>
                  )}
                </div>
                <p
                  className="text-sm leading-relaxed text-zinc-200"
                  onClick={() => seekTo(seg.start)}
                >
                  {seg.text}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SpeakersTab({
  speakers, speakerMetrics, timelineDuration, segments, currentTime, seekTo,
  speakerLinks, orgSpeakers, onSpeakerChanged, sessionIdStr,
}: {
  speakers: string[];
  speakerMetrics: Array<{ speaker: string; seconds: number; words: number; pct: number }>;
  timelineDuration: number;
  segments: TranscriptionSegment[];
  currentTime: number;
  seekTo: (t: number) => void;
  speakerLinks: SpeakerLinkRow[];
  orgSpeakers: OrgSpeaker[];
  onSpeakerChanged: () => void;
  sessionIdStr: string;
}) {
  if (speakers.length === 0) {
    return (
      <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 text-sm text-zinc-400 text-center">
        No speakers identified yet.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <SectionCard title="Speaker metrics" icon={Users}>
        <div className="space-y-3">
          {speakerMetrics.map((r, idx) => {
            const mm = Math.floor(r.seconds / 60);
            const ss = Math.floor(r.seconds % 60).toString().padStart(2, '0');
            const colorClass = PALETTE[idx % PALETTE.length];
            return (
              <div key={r.speaker}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm text-zinc-100 font-medium truncate">{r.speaker}</span>
                  <span className="text-[11px] text-zinc-400 tabular-nums whitespace-nowrap">
                    {mm}:{ss} <span className="text-zinc-600">·</span> {r.words.toLocaleString()}w
                    <span className="ml-2 text-zinc-100 font-semibold">{Math.round(r.pct)}%</span>
                  </span>
                </div>
                <div className="h-2 rounded-full bg-zinc-800 overflow-hidden">
                  <div
                    className={`${colorClass} h-full rounded-full transition-[width] duration-500`}
                    style={{ width: `${Math.min(100, Math.max(2, r.pct))}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      </SectionCard>

      {speakers.length > 1 && timelineDuration > 1 && (
        <SectionCard title="Timeline" icon={Clock}>
          <SpeakerTimeline
            duration={timelineDuration}
            segments={segments.map((s) => ({ start: s.start, end: s.end, speaker: s.speaker }))}
            speakers={speakers}
            currentTime={currentTime}
            onSeek={seekTo}
          />
        </SectionCard>
      )}

      <SectionCard title="Speaker assignments" icon={Edit2}>
        {speakerLinks.length === 0 ? (
          <p className="text-sm text-zinc-500">No speaker links recorded for this session.</p>
        ) : (
          <ul className="space-y-2">
            {speakerLinks.map((link) => {
              const labelText = link.speaker_display || link.raw_label;
              return (
                <li
                  key={link.id}
                  className="flex items-center justify-between gap-2 rounded-lg bg-zinc-950/40 border border-zinc-800 px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <SpeakerLabelEditor
                        speaker={labelText}
                        sessionId={sessionIdStr}
                        links={speakerLinks}
                        speakers={orgSpeakers}
                        onChanged={onSpeakerChanged}
                      />
                    </div>
                    <p className="text-[11px] text-zinc-500 mt-0.5 truncate">
                      Cluster: {link.raw_label}
                      {link.speaker_id ? ' · enrolled' : ' · not enrolled'}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </SectionCard>
    </div>
  );
}

function InsightsTab({ insights, insightsLoading }: { insights: any | null; insightsLoading: boolean }) {
  if (insightsLoading && !insights) {
    return (
      <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 flex items-center justify-center gap-3">
        <Loader2 className="w-4 h-4 text-fuchsia-400 animate-spin" />
        <span className="text-sm text-zinc-400">Loading insights…</span>
      </div>
    );
  }

  if (!insights) {
    return (
      <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 text-sm text-zinc-400 text-center">
        No insights available yet.
      </div>
    );
  }

  const keywords = Array.isArray(insights.keywords) ? insights.keywords : [];
  const topics = Array.isArray(insights.topics) ? insights.topics : [];
  const followUps = Array.isArray(insights.follow_ups || insights.followUps)
    ? (insights.follow_ups || insights.followUps)
    : [];
  const sentiment = insights.sentiment;

  return (
    <div className="space-y-3">
      {keywords.length > 0 && (
        <SectionCard title="Keywords" icon={TagIcon}>
          <div className="flex flex-wrap gap-2">
            {keywords.map((k: any, idx: number) => {
              const word = typeof k === 'string' ? k : (k?.word ?? '');
              if (!word) return null;
              return (
                <span
                  key={`${idx}-${word}`}
                  className="px-2.5 py-1 rounded-full bg-fuchsia-500/15 border border-fuchsia-500/30 text-fuchsia-200 text-xs"
                >
                  {word}
                </span>
              );
            })}
          </div>
        </SectionCard>
      )}

      {sentiment && (
        <SectionCard title="Sentiment" icon={Sparkles}>
          <div className="flex items-center gap-3">
            <div className="flex-1 h-2 rounded-full bg-zinc-800 overflow-hidden">
              <div
                className={`h-full rounded-full ${
                  sentiment.overall === 'positive' ? 'bg-emerald-500' :
                  sentiment.overall === 'negative' ? 'bg-red-500' :
                  'bg-amber-500'
                }`}
                style={{ width: `${Math.round((sentiment.score ?? 0.5) * 100)}%` }}
              />
            </div>
            <span className="text-xs text-zinc-300 capitalize">
              {sentiment.overall || 'neutral'}
            </span>
          </div>
        </SectionCard>
      )}

      {topics.length > 0 && (
        <SectionCard title="Topics" icon={Brain}>
          <ul className="space-y-2">
            {topics.map((t: any, idx: number) => {
              const label = typeof t === 'string' ? t : (t?.label || t?.name || t?.topic || '');
              if (!label) return null;
              return (
                <li key={idx} className="text-sm text-zinc-200 flex items-start gap-2">
                  <span className="text-indigo-400 mt-1">•</span>
                  <span>{label}</span>
                </li>
              );
            })}
          </ul>
        </SectionCard>
      )}

      {followUps.length > 0 && (
        <SectionCard title="Follow-ups" icon={RefreshCw}>
          <ul className="space-y-2">
            {followUps.map((f: any, idx: number) => {
              const text = typeof f === 'string' ? f : (f?.text || f?.action || JSON.stringify(f));
              return (
                <li key={idx} className="text-sm text-zinc-200 flex items-start gap-2">
                  <span className="text-amber-400 mt-1">→</span>
                  <span>{text}</span>
                </li>
              );
            })}
          </ul>
        </SectionCard>
      )}

      {keywords.length === 0 && topics.length === 0 && followUps.length === 0 && !sentiment && (
        <div className="rounded-2xl bg-zinc-900/60 border border-zinc-800 p-6 text-sm text-zinc-400 text-center">
          The Summary tab covers the highlights for this meeting. Re-process to refresh insights.
        </div>
      )}
    </div>
  );
}

function PeopleTagsTab(props: {
  participants: Participant[];
  newParticipantName: string;
  setNewParticipantName: (s: string) => void;
  newParticipantEmail: string;
  setNewParticipantEmail: (s: string) => void;
  newParticipantRole: string;
  setNewParticipantRole: (s: string) => void;
  participantBusy: boolean;
  participantError: string | null;
  editingParticipantId: string | null;
  editParticipantName: string;
  setEditParticipantName: (s: string) => void;
  editParticipantEmail: string;
  setEditParticipantEmail: (s: string) => void;
  editParticipantRole: string;
  setEditParticipantRole: (s: string) => void;
  startEditParticipant: (p: Participant) => void;
  cancelEditParticipant: () => void;
  saveEditParticipant: () => void;
  addParticipant: () => void;
  deleteParticipant: (id: string) => void;
  contactSearchParticipantId: string | null;
  contactSearchQuery: string;
  setContactSearchQuery: (s: string) => void;
  contactSearchResults: ContactOpsPerson[];
  contactSearchLoading: boolean;
  contactSearchAmbiguous: boolean;
  openContactSearch: (participant: Participant) => void;
  closeContactSearch: () => void;
  stampParticipantContact: (participantId: string, person: ContactOpsPerson | null) => void;
  tags: string[];
  newTag: string;
  setNewTag: (s: string) => void;
  tagBusy: boolean;
  tagError: string | null;
  addTag: () => void;
  removeTag: (tag: string) => void;
  setTagError: (s: string | null) => void;
}) {
  const {
    participants,
    newParticipantName, setNewParticipantName,
    newParticipantEmail, setNewParticipantEmail,
    newParticipantRole, setNewParticipantRole,
    participantBusy, participantError,
    editingParticipantId,
    editParticipantName, setEditParticipantName,
    editParticipantEmail, setEditParticipantEmail,
    editParticipantRole, setEditParticipantRole,
    startEditParticipant, cancelEditParticipant, saveEditParticipant,
    addParticipant, deleteParticipant,
    contactSearchParticipantId, contactSearchQuery, setContactSearchQuery,
    contactSearchResults, contactSearchLoading, contactSearchAmbiguous,
    openContactSearch, closeContactSearch, stampParticipantContact,
    tags, newTag, setNewTag, tagBusy, tagError, addTag, removeTag, setTagError,
  } = props;

  return (
    <div className="space-y-3">
      <SectionCard title="Tags" icon={TagIcon}>
        <div className="flex flex-wrap items-center gap-1.5">
          {[...tags].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase())).map((tag) => (
            <TagChip key={tag} tag={tag} onRemove={() => removeTag(tag)} />
          ))}
          {tags.length === 0 && (
            <span className="text-xs text-zinc-500">No tags yet.</span>
          )}
        </div>
        <div className="mt-3 flex items-center gap-2">
          <input
            type="text"
            value={newTag}
            onChange={(e) => setNewTag(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); addTag(); }
              if (e.key === 'Escape') { setNewTag(''); setTagError(null); }
            }}
            placeholder="Add tag"
            maxLength={40}
            disabled={tagBusy}
            className="flex-1 px-3 py-2 rounded-lg bg-zinc-950/60 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
          />
          <button
            type="button"
            onClick={addTag}
            disabled={tagBusy || !newTag.trim()}
            className="px-3 py-2 rounded-lg bg-fuchsia-600 text-white text-sm font-medium disabled:opacity-50"
          >
            Add
          </button>
        </div>
        {tagError && (
          <p className="mt-2 text-xs text-red-300">{tagError}</p>
        )}
      </SectionCard>

      <SectionCard title={`Participants${participants.length ? ` (${participants.length})` : ''}`} icon={Users}>
        {participantError && (
          <div className="mb-2 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
            {participantError}
          </div>
        )}
        {participants.length === 0 ? (
          <p className="text-sm text-zinc-500 mb-3">No participants yet.</p>
        ) : (
          <ul className="space-y-2 mb-3">
            {participants.map((p) => (
              <li key={p.id} className="rounded-lg bg-zinc-950/40 border border-zinc-800 px-3 py-2">
                {editingParticipantId === p.id ? (
                  <div className="space-y-2">
                    <input
                      type="text"
                      value={editParticipantName}
                      onChange={(e) => setEditParticipantName(e.target.value)}
                      placeholder="Name"
                      className="w-full px-2.5 py-1.5 rounded bg-zinc-900 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
                    />
                    <input
                      type="email"
                      value={editParticipantEmail}
                      onChange={(e) => setEditParticipantEmail(e.target.value)}
                      placeholder="Email (optional)"
                      className="w-full px-2.5 py-1.5 rounded bg-zinc-900 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
                    />
                    <input
                      type="text"
                      value={editParticipantRole}
                      onChange={(e) => setEditParticipantRole(e.target.value)}
                      placeholder="Role (optional)"
                      className="w-full px-2.5 py-1.5 rounded bg-zinc-900 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
                    />
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={saveEditParticipant}
                        disabled={participantBusy || !editParticipantName.trim()}
                        className="px-3 py-1.5 rounded bg-fuchsia-600 text-white text-xs font-medium disabled:opacity-50"
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        onClick={cancelEditParticipant}
                        disabled={participantBusy}
                        className="px-3 py-1.5 rounded bg-zinc-900 border border-zinc-800 text-xs text-zinc-200"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <p className="truncate text-sm font-medium text-zinc-100">{p.name}</p>
                        {p.role && (
                          <span className="px-1.5 py-0.5 rounded bg-fuchsia-500/15 border border-fuchsia-500/30 text-fuchsia-200 text-[10px]">
                            {p.role}
                          </span>
                        )}
                      </div>
                      {p.email && <p className="truncate text-xs text-zinc-500">{p.email}</p>}
                      {p.contact_id && (
                        <p className="mt-1 text-xs font-medium text-emerald-300">
                          {p.contact_link_source === 'manual'
                            ? 'Contact-Ops: Manually linked'
                            : 'Contact-Ops linked'}
                        </p>
                      )}
                      {contactSearchParticipantId === p.id && (
                        <div className="mt-2 space-y-2">
                          <div className="relative">
                            <Search className="pointer-events-none absolute left-2 top-2 h-3.5 w-3.5 text-zinc-500" />
                            <input
                              type="text"
                              value={contactSearchQuery}
                              onChange={(e) => setContactSearchQuery(e.target.value)}
                              placeholder="Search Contact-Ops"
                              className="w-full rounded border border-zinc-700 bg-zinc-900 py-1 pl-7 pr-2 text-sm text-zinc-100"
                            />
                          </div>
                          {contactSearchLoading && <p className="text-xs text-zinc-400">Searching…</p>}
                          {contactSearchAmbiguous && (
                            <p className="rounded bg-amber-500/10 px-2 py-1 text-xs text-amber-200">
                              Multiple results match. Choose a person manually; no link is selected automatically.
                            </p>
                          )}
                          {!contactSearchLoading && contactSearchQuery.trim().length >= 2 && contactSearchResults.length === 0 && (
                            <p className="text-xs text-zinc-400">No matches</p>
                          )}
                          {contactSearchResults.map((person) => (
                            <button
                              key={person.person_id}
                              type="button"
                              onClick={() => stampParticipantContact(p.id, person)}
                              disabled={participantBusy}
                              className="block w-full rounded border border-zinc-700 px-2 py-1.5 text-left text-sm text-zinc-100 active:bg-zinc-800 disabled:opacity-50"
                            >
                              <span className="block truncate font-medium">{person.display_name}</span>
                              <span className="block text-xs text-zinc-400">Choose manually</span>
                            </button>
                          ))}
                          <div className="flex gap-2">
                            {p.contact_id && (
                              <button
                                type="button"
                                onClick={() => stampParticipantContact(p.id, null)}
                                disabled={participantBusy}
                                className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-200 disabled:opacity-50"
                              >
                                Clear link
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={closeContactSearch}
                              disabled={participantBusy}
                              className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-200 disabled:opacity-50"
                            >
                              Done
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <button
                        type="button"
                        onClick={() => openContactSearch(p)}
                        className="rounded p-1.5 text-emerald-300 active:bg-emerald-500/20"
                        aria-label="Tag Contact-Ops person"
                      >
                        <Link2 className="w-3.5 h-3.5" />
                      </button>
                      <button
                        type="button"
                        onClick={() => startEditParticipant(p)}
                        className="rounded p-1.5 text-zinc-400 active:bg-zinc-800"
                        aria-label="Edit participant"
                      >
                        <Edit2 className="w-3.5 h-3.5" />
                      </button>
                      <button
                        type="button"
                        onClick={() => deleteParticipant(p.id)}
                        className="rounded p-1.5 text-zinc-400 active:bg-red-500/20 active:text-red-300"
                        aria-label="Remove participant"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}

        <div className="space-y-2 border-t border-zinc-800 pt-3">
          <p className="text-[11px] uppercase tracking-wider text-zinc-500">Add participant</p>
          <input
            type="text"
            value={newParticipantName}
            onChange={(e) => setNewParticipantName(e.target.value)}
            placeholder="Name (required)"
            className="w-full px-2.5 py-2 rounded-lg bg-zinc-950/60 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
          />
          <input
            type="email"
            value={newParticipantEmail}
            onChange={(e) => setNewParticipantEmail(e.target.value)}
            placeholder="Email (optional)"
            className="w-full px-2.5 py-2 rounded-lg bg-zinc-950/60 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
          />
          <input
            type="text"
            value={newParticipantRole}
            onChange={(e) => setNewParticipantRole(e.target.value)}
            placeholder="Role (e.g. host)"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newParticipantName.trim() && !participantBusy) {
                addParticipant();
              }
            }}
            className="w-full px-2.5 py-2 rounded-lg bg-zinc-950/60 border border-zinc-800 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500"
          />
          <button
            type="button"
            onClick={addParticipant}
            disabled={participantBusy || !newParticipantName.trim()}
            className="w-full px-3 py-2 rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 text-white text-sm font-medium disabled:opacity-50"
          >
            Add participant
          </button>
        </div>
      </SectionCard>
    </div>
  );
}
