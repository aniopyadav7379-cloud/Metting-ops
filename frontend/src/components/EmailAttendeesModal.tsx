import React, { useEffect, useMemo, useState } from 'react';
import { X, Mail, RefreshCw, Send, AlertCircle, Check, Plus } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';

interface SpeakerLink {
  id: number;
  raw_label: string;
  speaker_id: number | null;
  speaker?: {
    id: number;
    display_name: string;
    email?: string | null;
  } | null;
}

interface SpeakerOption {
  id: number;
  display_name: string;
  email?: string | null;
}

interface Props {
  sessionId: string | number;
  isOpen: boolean;
  onClose: () => void;
}

interface SendResult {
  sent: number;
  skipped: number;
  failures: Array<{
    speaker_id?: number;
    participant_id?: number | string;
    external?: boolean;
    name?: string;
    reason: string;
  }>;
}

type ReportBrandMode = 'default' | 'meeting_ops' | 'workspace' | 'unbranded';

// Client-side sanity check. The server uses pydantic EmailStr for the
// strict validation; this just keeps obvious typos out of the chip list.
const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export const EmailAttendeesModal: React.FC<Props> = ({ sessionId, isOpen, onClose }) => {
  const { activeOrganization } = useOrg();
  const [links, setLinks] = useState<SpeakerLink[]>([]);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SendResult | null>(null);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [includeLink, setIncludeLink] = useState(false);
  const [includeSummary, setIncludeSummary] = useState(true);
  const [includeTranscript, setIncludeTranscript] = useState(false);
  const [brandMode, setBrandMode] = useState<ReportBrandMode>('default');
  const [customMessage, setCustomMessage] = useState('');

  // Free-form external recipients (people NOT identified as speakers in
  // this meeting). A collaborator grant is created only if the sender
  // explicitly chooses the ongoing-access link.
  const [externalRecipients, setExternalRecipients] = useState<string[]>([]);
  const [externalDraft, setExternalDraft] = useState('');
  const [externalError, setExternalError] = useState<string | null>(null);

  const orgHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = {};
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
    return headers;
  };

  useEffect(() => {
    if (!isOpen) return;
    // Reset the whole composer every time it opens so recipients/content
    // from another meeting or a previous send never carry over.
    setExternalRecipients([]);
    setExternalDraft('');
    setExternalError(null);
    setSelected(new Set());
    setIncludeLink(false);
    setIncludeSummary(true);
    setIncludeTranscript(false);
    setBrandMode('default');
    setCustomMessage('');
    setResult(null);
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [linksRes, speakersRes] = await Promise.all([
          fetch(
            `${config.apiBaseUrl}/api/sessions/${sessionId}/speaker-links`,
            { headers: orgHeaders() },
          ),
          fetch(
            `${config.apiBaseUrl}/api/speakers?session_id=${encodeURIComponent(String(sessionId))}`,
            { headers: orgHeaders() },
          ),
        ]);
        if (cancelled) return;
        if (linksRes.ok && speakersRes.ok) {
          const linksData: SpeakerLink[] = await linksRes.json();
          const speakersData: SpeakerOption[] = await speakersRes.json();
          const speakersById = new Map(
            (Array.isArray(speakersData) ? speakersData : []).map((speaker) => [
              speaker.id,
              speaker,
            ]),
          );
          const hydratedLinks = (Array.isArray(linksData) ? linksData : []).map((link) => {
            const speaker = link.speaker_id
              ? speakersById.get(link.speaker_id) || null
              : null;
            return {
              ...link,
              speaker: speaker
                ? {
                    id: speaker.id,
                    display_name: speaker.display_name,
                    email: speaker.email,
                  }
                : null,
            };
          });
          setLinks(hydratedLinks);
          // Default-select every speaker who has an email on file.
          const auto = new Set<number>();
          for (const l of hydratedLinks) {
            if (l.speaker_id && l.speaker?.email) auto.add(l.speaker_id as number);
          }
          setSelected(auto);
        } else {
          const status = !linksRes.ok ? linksRes.status : speakersRes.status;
          setError(`Couldn't load attendee list (HTTP ${status}).`);
        }
      } catch {
        setError('Network error loading speakers.');
      }
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, sessionId, activeOrganization?.slug]);

  const namedSpeakers = useMemo(
    () =>
      links
        .filter((l) => l.speaker_id && l.speaker)
        .map((l) => ({
          link_id: l.id,
          speaker_id: l.speaker_id!,
          name: l.speaker?.display_name || l.raw_label,
          email: l.speaker?.email || '',
        })),
    [links],
  );

  const unmappedLinks = links.filter((l) => !l.speaker_id);

  const toggle = (id: number) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Set of speaker emails already in the named-attendee picker. We use it
  // to short-circuit duplicates if the user types one of those addresses
  // into the external-recipients box. The backend dedupes too, but doing
  // it here keeps the UI honest.
  const speakerEmailSet = useMemo(() => {
    const out = new Set<string>();
    for (const s of namedSpeakers) {
      const e = (s.email || '').trim().toLowerCase();
      if (e) out.add(e);
    }
    return out;
  }, [namedSpeakers]);

  const addExternalRecipient = () => {
    const raw = externalDraft.trim();
    if (!raw) {
      setExternalError(null);
      return;
    }
    if (!EMAIL_REGEX.test(raw)) {
      setExternalError('That doesn’t look like an email address.');
      return;
    }
    const norm = raw.toLowerCase();
    if (externalRecipients.some((e) => e.toLowerCase() === norm)) {
      setExternalError('Already added.');
      return;
    }
    if (speakerEmailSet.has(norm)) {
      setExternalError('That address is already in the speaker list above.');
      return;
    }
    setExternalRecipients((cur) => [...cur, raw]);
    setExternalDraft('');
    setExternalError(null);
  };

  const removeExternalRecipient = (email: string) => {
    setExternalRecipients((cur) => cur.filter((e) => e !== email));
  };

  const totalRecipientCount = selected.size + externalRecipients.length;

  const handleSend = async () => {
    if (totalRecipientCount === 0) {
      setError('Add at least one recipient — a speaker or an external email.');
      return;
    }
    const include: string[] = [];
    if (includeLink) include.push('link');
    if (includeSummary) include.push('summary_pdf');
    if (includeTranscript) include.push('transcript_pdf');
    if (include.length === 0) {
      setError('Pick at least one thing to include (link or attachments).');
      return;
    }
    setSending(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}/email-attendees`,
        {
          method: 'POST',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({
            speaker_ids: Array.from(selected),
            additional_recipients: externalRecipients,
            include,
            brand_mode: brandMode,
            message: customMessage.trim() || undefined,
          }),
        },
      );
      const data = await res.json();
      if (res.ok) {
        setResult(data as SendResult);
      } else {
        setError(`Send failed (HTTP ${res.status}): ${(data?.detail || JSON.stringify(data)).toString().slice(0, 200)}`);
      }
    } catch {
      setError('Network error during send.');
    }
    setSending(false);
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6">
      <div className="w-full max-w-2xl rounded-lg border border-gray-700 bg-gray-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-white">Share via email</h2>
            <p className="mt-0.5 text-xs text-gray-400">
              Send a static report copy, or explicitly add ongoing meeting access.
            </p>
          </div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto px-5 py-4 space-y-5">
          {loading && (
            <div className="flex items-center gap-2 text-sm text-gray-400">
              <RefreshCw className="h-4 w-4 animate-spin" />
              Loading attendee list…
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 rounded border border-red-700 bg-red-900/30 px-3 py-2 text-xs text-red-300">
              <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {result && (
            <div className="space-y-2 rounded border border-emerald-700 bg-emerald-900/20 px-3 py-2 text-sm text-emerald-200">
              <div className="flex items-center gap-2">
                <Check className="h-4 w-4" />
                Sent {result.sent} email{result.sent === 1 ? '' : 's'}. Skipped {result.skipped}.
              </div>
              {result.failures.length > 0 && (
                <ul className="ml-5 list-disc text-xs text-amber-300">
                  {result.failures.map((f, idx) => {
                    const label =
                      f.name ||
                      (f.speaker_id ? `speaker ${f.speaker_id}` : null) ||
                      (f.external ? 'external recipient' : `recipient ${idx + 1}`);
                    return (
                      <li key={idx}>
                        <span className="font-medium">{label}</span>: {f.reason}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}

          {!loading && !result && (
            <>
              <div>
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
                  Speakers identified in this meeting
                </p>
                <div className="space-y-1.5">
                  {namedSpeakers.length === 0 && (
                    <p className="text-xs text-gray-500">
                      No named speakers on this meeting yet. Open the Speakers tab to
                      listen to samples and identify them, or add recipients below.
                    </p>
                  )}
                  {namedSpeakers.map((s) => {
                    const checked = selected.has(s.speaker_id);
                    const missing = !s.email;
                    return (
                      <label
                        key={s.speaker_id}
                        className="flex items-center gap-3 rounded-md border border-gray-800 bg-gray-900 px-3 py-2 text-sm cursor-pointer hover:border-gray-700"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={missing}
                          onChange={() => toggle(s.speaker_id)}
                          className="h-3.5 w-3.5"
                        />
                        <div className="flex-1 min-w-0">
                          <div className="font-medium text-white truncate">{s.name}</div>
                          {missing ? (
                            <div className="text-[11px] text-amber-300">
                              No email on file — update this person in the Speaker Library.
                            </div>
                          ) : (
                            <div className="text-[11px] text-gray-500 truncate">{s.email}</div>
                          )}
                        </div>
                      </label>
                    );
                  })}
                  {unmappedLinks.length > 0 && (
                    <p className="text-[11px] text-gray-500 pt-1">
                      {unmappedLinks.length} unnamed cluster{unmappedLinks.length === 1 ? '' : 's'} ({unmappedLinks.map((l) => l.raw_label).join(', ')}) — identify them in the Speakers tab first.
                    </p>
                  )}
                </div>
              </div>

              <div>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-400">
                  Other recipients
                </p>
                <p className="mb-2 text-[11px] text-gray-500">
                  Send a static report to people who weren’t identified as speakers.
                  They receive ongoing access only if you select the access link below.
                </p>
                {externalRecipients.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {externalRecipients.map((email) => (
                      <span
                        key={email}
                        className="inline-flex items-center gap-1 rounded-full border border-purple-700 bg-purple-900/30 px-2.5 py-1 text-xs text-purple-100"
                      >
                        <Mail className="h-3 w-3" />
                        {email}
                        <button
                          type="button"
                          aria-label={`Remove ${email}`}
                          onClick={() => removeExternalRecipient(email)}
                          className="ml-0.5 rounded p-0.5 text-purple-200 hover:bg-purple-800/40 hover:text-white"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                <div className="flex gap-2">
                  <input
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    value={externalDraft}
                    onChange={(e) => {
                      setExternalDraft(e.target.value);
                      if (externalError) setExternalError(null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ',' || e.key === 'Tab') {
                        if (externalDraft.trim()) {
                          e.preventDefault();
                          addExternalRecipient();
                        }
                      }
                    }}
                    placeholder="email@example.com"
                    className="flex-1 rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
                  />
                  <button
                    type="button"
                    onClick={addExternalRecipient}
                    disabled={!externalDraft.trim()}
                    className="inline-flex items-center gap-1 rounded border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50"
                  >
                    <Plus className="h-3.5 w-3.5" />
                    Add
                  </button>
                </div>
                {externalError && (
                  <p className="mt-1 text-[11px] text-amber-300">{externalError}</p>
                )}
              </div>

              <div className="rounded-lg border border-gray-800 bg-gray-900 p-3">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
                  Choose the outcome
                </p>
                <div className="space-y-1.5">
                  <label className="flex items-center gap-2 text-sm text-gray-200 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={includeLink}
                      onChange={(e) => setIncludeLink(e.target.checked)}
                      className="h-3.5 w-3.5"
                    />
                    Ongoing access link (revocable)
                  </label>
                  {includeLink && (
                    <p className="ml-5 text-[11px] text-amber-300">
                      Creates or rotates a collaborator invitation. The recipient can
                      return to the live meeting record until access is revoked.
                    </p>
                  )}
                  <label className="flex items-center gap-2 text-sm text-gray-200 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={includeSummary}
                      onChange={(e) => setIncludeSummary(e.target.checked)}
                      className="h-3.5 w-3.5"
                    />
                    Static summary copy (PDF attachment)
                  </label>
                  <label className="flex items-center gap-2 text-sm text-gray-200 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={includeTranscript}
                      onChange={(e) => setIncludeTranscript(e.target.checked)}
                      className="h-3.5 w-3.5"
                    />
                    Static full transcript copy (PDF attachment)
                  </label>
                  <label className="block pt-2 text-xs font-medium text-gray-400">
                    Attachment branding
                    <select
                      value={brandMode}
                      onChange={(event) =>
                        setBrandMode(event.target.value as ReportBrandMode)
                      }
                      className="mt-1 w-full rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-200"
                    >
                      <option value="default">Workspace default</option>
                      <option value="meeting_ops">Meeting-Ops</option>
                      <option value="workspace">Workspace white-label</option>
                      <option value="unbranded">No logo or heading</option>
                    </select>
                  </label>
                </div>
              </div>

              <div className="rounded-lg border border-gray-800 bg-gray-900 p-3">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
                  Note (optional)
                </p>
                <textarea
                  rows={3}
                  value={customMessage}
                  onChange={(e) => setCustomMessage(e.target.value)}
                  placeholder="Add a short message that appears at the top of the email…"
                  className="w-full rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
                />
                <p className="mt-1 text-[11px] text-gray-500">
                  Replies go straight to you, not to a shared inbox.
                </p>
              </div>
            </>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-gray-800 px-5 py-3">
          <span className="text-xs text-gray-500">
            {result
              ? 'Send another batch by closing and reopening this dialog.'
              : `${totalRecipientCount} recipient${totalRecipientCount === 1 ? '' : 's'} selected${
                  externalRecipients.length
                    ? ` (${selected.size} speaker${selected.size === 1 ? '' : 's'} + ${externalRecipients.length} external)`
                    : ''
                }`}
          </span>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded border border-gray-700 px-4 py-2 text-sm text-gray-300 hover:bg-gray-800"
            >
              {result ? 'Done' : 'Cancel'}
            </button>
            {!result && (
              <button
                onClick={handleSend}
                disabled={sending || totalRecipientCount === 0}
                className="inline-flex items-center gap-2 rounded bg-purple-600 px-4 py-2 text-sm font-medium text-white hover:bg-purple-500 disabled:opacity-50"
              >
                {sending ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default EmailAttendeesModal;
