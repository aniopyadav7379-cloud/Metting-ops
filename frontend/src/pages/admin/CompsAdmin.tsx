import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Rocket, RefreshCw, Ticket, Gift, CircleCheck, XCircle, Clock,
  Copy, Download, Search, ShieldAlert, UserPlus, Ban, Crown, CreditCard,
  Plus, Mail, Send, Upload, Loader2, Sparkles,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { config } from '../../config';
import { useAuth } from '../../contexts/AuthContext';
import { showToast } from '../../components/Toast';
import { showConfirm } from '../../utils/notifications';

// ---------------------------------------------------------------------------
// Types (mirror backend api/admin_comps.py + api/invite_codes.py responses)
// ---------------------------------------------------------------------------
type CompStatus = 'active' | 'expired' | 'permanent' | 'superuser';

interface CompUser {
  id: number;
  email: string;
  username: string;
  full_name?: string | null;
  tier: string;
  status: CompStatus;
  tier_expires_at?: string | null;
  days_remaining?: number | null;
  is_founding_member: boolean;
  founding_cohort?: string | null;
  has_stripe: boolean;
  personal_org_slug?: string | null;
  personal_org_plan?: string | null;
  created_at?: string | null;
}

interface CompsSummary {
  total_nonfree: number;
  active_comps: number;
  expired_pending_revert: number;
  permanent: number;
  superusers: number;
  by_cohort: Record<string, number>;
  codes_total: number;
  codes_redeemed: number;
  codes_available: number;
}

interface InviteCodeAdminItem {
  code: string;
  is_active: boolean;
  redeemed: boolean;
  redeemed_by_email?: string | null;
  redeemed_at?: string | null;
  emailed_at?: string | null;
  created_by_email?: string | null;
  note?: string | null;
  cohort?: string | null;
  created_at?: string | null;
}

interface CompSnapshot {
  email: string;
  tier: string;
  tier_expires_at?: string | null;
  personal_org_plan?: string | null;
}
interface CompActionResponse {
  action: string;
  before: CompSnapshot;
  after: CompSnapshot;
}

interface InviteCodeEmailResult {
  code: string;
  email: string;
  status: string;
  subject?: string | null;
  text_body?: string | null;
  html_body?: string | null;
  error?: string | null;
  emailed_at?: string | null;
}

interface InviteCodeEmailResponse {
  dry_run: boolean;
  total: number;
  sent: number;
  skipped: number;
  failed: number;
  results: InviteCodeEmailResult[];
}

// ---------------------------------------------------------------------------
// API helpers (JWT + org headers auto-injected by the global fetch interceptor)
// ---------------------------------------------------------------------------
async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${config.apiBaseUrl}${path}`, {
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${config.apiBaseUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* body not JSON */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

function relative(iso?: string | null): string {
  if (!iso) return '—';
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

async function copy(text: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    showToast.success(label);
  } catch {
    showToast.error('Clipboard blocked by the browser.');
  }
}

function normalizeEmailList(raw: string): string[] {
  const seen = new Set<string>();
  const emails: string[] = [];
  raw
    .split(/\r?\n|,/)
    .map((value) => value.trim())
    .filter(Boolean)
    .forEach((value) => {
      const firstCell = value.split(';', 1)[0].trim().replace(/^["']|["']$/g, '');
      if (!firstCell) return;
      const lowered = firstCell.toLowerCase();
      if (lowered === 'email' || lowered === 'recipient' || lowered === 'recipients') return;
      if (seen.has(lowered)) return;
      seen.add(lowered);
      emails.push(lowered);
    });
  return emails;
}

function emailPreviewKey(
  mode: 'single' | 'batch',
  code: string,
  cohort: string,
  recipients: string[],
): string {
  return JSON.stringify({ mode, code, cohort: cohort.trim(), recipients });
}

// ---------------------------------------------------------------------------
// Small presentational bits
// ---------------------------------------------------------------------------
const CARD = 'rounded-lg border border-zinc-800 bg-zinc-900/70 p-4 sm:p-6';

function SummaryCard({ icon, label, value, tone }: {
  icon: React.ReactNode; label: string; value: number | string; tone: string;
}) {
  return (
    <div className={`${CARD} flex items-center gap-3`}>
      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${tone}`}>
        {icon}
      </div>
      <div className="min-w-0">
        <div className="text-2xl font-semibold leading-tight">{value}</div>
        <div className="truncate text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      </div>
    </div>
  );
}

function CompStatusBadge({ status, days }: { status: CompStatus; days?: number | null }) {
  if (status === 'active') {
    const soon = typeof days === 'number' && days <= 5;
    return (
      <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
        soon ? 'bg-amber-500/15 text-amber-300' : 'bg-emerald-500/15 text-emerald-300'
      }`}>
        <CircleCheck className="h-3.5 w-3.5" />
        {typeof days === 'number' ? `${days}d left` : 'active'}
      </span>
    );
  }
  if (status === 'expired') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-2.5 py-1 text-xs font-medium text-red-300">
        <Clock className="h-3.5 w-3.5" /> expired
      </span>
    );
  }
  if (status === 'superuser') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-fuchsia-500/15 px-2.5 py-1 text-xs font-medium text-fuchsia-300">
        <Crown className="h-3.5 w-3.5" /> superuser
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-sky-500/15 px-2.5 py-1 text-xs font-medium text-sky-300">
      <CreditCard className="h-3.5 w-3.5" /> permanent
    </span>
  );
}

function CodeStatusBadge({ c }: { c: InviteCodeAdminItem }) {
  if (c.redeemed) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-zinc-700/50 px-2.5 py-1 text-xs font-medium text-zinc-300">
        <CircleCheck className="h-3.5 w-3.5" /> redeemed
      </span>
    );
  }
  if (c.emailed_at) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-indigo-500/15 px-2.5 py-1 text-xs font-medium text-indigo-300">
        <Mail className="h-3.5 w-3.5" /> emailed
      </span>
    );
  }
  if (!c.is_active) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-2.5 py-1 text-xs font-medium text-red-300">
        <XCircle className="h-3.5 w-3.5" /> inactive
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2.5 py-1 text-xs font-medium text-emerald-300">
      <Ticket className="h-3.5 w-3.5" /> available
    </span>
  );
}

const BTN_PRIMARY =
  'inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-purple-600 to-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:from-purple-500 hover:to-indigo-500 disabled:opacity-50';
const BTN_OUTLINE =
  'inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 transition-colors hover:bg-zinc-800 disabled:opacity-50';
const INPUT =
  'rounded-lg border border-zinc-700 bg-zinc-950/70 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:border-indigo-500 focus:outline-none';

type InviteEmailMode = 'single' | 'batch';

function StatusPill({ status }: { status: string }) {
  const tone =
    status === 'sent'
      ? 'bg-emerald-500/15 text-emerald-300'
      : status === 'preview'
        ? 'bg-sky-500/15 text-sky-300'
        : status === 'skipped_emailed'
          ? 'bg-zinc-700/60 text-zinc-300'
          : 'bg-red-500/15 text-red-300';
  return (
    <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}

function InviteCodeEmailModal({
  open,
  mode,
  initialCode,
  initialCohort,
  availableCohorts,
  onClose,
  onRefresh,
}: {
  open: boolean;
  mode: InviteEmailMode;
  initialCode?: string | null;
  initialCohort?: string | null;
  availableCohorts: string[];
  onClose: () => void;
  onRefresh: () => Promise<void>;
}) {
  const [recipientInput, setRecipientInput] = useState('');
  const [cohort, setCohort] = useState(initialCohort ?? '');
  const [preview, setPreview] = useState<InviteCodeEmailResponse | null>(null);
  const [previewKey, setPreviewKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setRecipientInput('');
    setCohort(initialCohort ?? '');
    setPreview(null);
    setPreviewKey('');
    setError(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, [open, initialCode, initialCohort, mode]);

  const recipients = useMemo(() => {
    const parsed = normalizeEmailList(recipientInput);
    return mode === 'single' ? parsed.slice(0, 1) : parsed;
  }, [mode, recipientInput]);

  const currentKey = useMemo(
    () => emailPreviewKey(mode, initialCode ?? '', cohort, recipients),
    [mode, initialCode, cohort, recipients],
  );
  const previewMatches = Boolean(preview) && previewKey === currentKey;

  const uploadCsv = async (file: File | null) => {
    if (!file) return;
    try {
      const text = await file.text();
      setRecipientInput(text);
      setPreview(null);
      setPreviewKey('');
    } catch (err) {
      showToast.error(`CSV import failed: ${(err as Error).message}`);
    }
  };

  const run = async (dryRun: boolean) => {
    setError(null);
    if (!initialCode && mode === 'single') {
      setError('No invite code selected.');
      return;
    }
    if (!recipients.length) {
      setError('Enter at least one recipient email.');
      return;
    }
    if (mode === 'batch' && !cohort.trim()) {
      setError('Choose a cohort.');
      return;
    }

    const path = mode === 'single'
      ? '/api/admin/invite-codes/send'
      : '/api/admin/invite-codes/send-cohort';
    const body = mode === 'single'
      ? {
          dry_run: dryRun,
          recipients: [{ email: recipients[0], code: initialCode ?? '' }],
        }
      : {
          dry_run: dryRun,
          cohort: cohort.trim(),
          recipients,
        };

    setBusy(true);
    try {
      const res = await apiPost<InviteCodeEmailResponse>(path, body);
      setPreview(res);
      setPreviewKey(currentKey);
      if (dryRun) {
        showToast.success(`Previewed ${res.total} email${res.total === 1 ? '' : 's'}.`);
      } else {
        showToast.success(
          res.failed
            ? `Sent ${res.sent}, skipped ${res.skipped}, failed ${res.failed}.`
            : `Sent ${res.sent} invite email${res.sent === 1 ? '' : 's'}.`,
        );
        await onRefresh();
      }
    } catch (err) {
      const message = (err as Error).message;
      setError(message);
      showToast.error(`Email send failed: ${message}`);
    } finally {
      setBusy(false);
    }
  };

  const confirmSend = async () => {
    if (!previewMatches || !preview) {
      showToast.error('Run a fresh preview before sending.');
      return;
    }
    const ok = await showConfirm(
      `Send ${preview.total} invite email${preview.total === 1 ? '' : 's'} now?`,
      { confirmLabel: 'Send', tone: 'danger' },
    );
    if (!ok) return;
    await run(false);
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 py-6">
      <div className="max-h-[90vh] w-full max-w-4xl overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl shadow-black/60">
        <div className="flex items-start justify-between border-b border-zinc-800 px-5 py-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-medium text-zinc-200">
              <Mail className="h-4 w-4 text-indigo-300" />
              {mode === 'single' ? 'Email invite code' : 'Email codes'}
            </div>
            <div className="mt-1 text-xs text-zinc-500">
              {mode === 'single'
                ? 'Preview first, then confirm before Postmark send.'
                : 'Select a cohort, preview the code-to-recipient mapping, then confirm.'}
            </div>
          </div>
          <button className={BTN_OUTLINE} onClick={onClose}>
            Close
          </button>
        </div>

        <div className="grid gap-0 lg:grid-cols-[360px_1fr]">
          <div className="border-b border-zinc-800 p-5 lg:border-b-0 lg:border-r">
            <div className="space-y-4">
              {mode === 'single' ? (
                <div>
                  <label className="mb-2 block text-xs uppercase tracking-wide text-zinc-500">Invite code</label>
                  <input className={`${INPUT} w-full`} value={initialCode ?? ''} disabled />
                </div>
              ) : (
                <div>
                  <label className="mb-2 block text-xs uppercase tracking-wide text-zinc-500">Cohort</label>
                  <select className={`${INPUT} w-full`} value={cohort} onChange={(e) => { setCohort(e.target.value); setPreview(null); setPreviewKey(''); }}>
                    <option value="">Select a cohort</option>
                    {availableCohorts.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                </div>
              )}

              <div>
                <label className="mb-2 block text-xs uppercase tracking-wide text-zinc-500">
                  {mode === 'single' ? 'Recipient email' : 'Recipient emails'}
                </label>
                {mode === 'single' ? (
                  <input
                    type="email"
                    className={`${INPUT} w-full`}
                    placeholder="recipient@example.com"
                    value={recipientInput}
                    onChange={(e) => { setRecipientInput(e.target.value); setPreview(null); setPreviewKey(''); }}
                  />
                ) : (
                  <textarea
                    className={`${INPUT} min-h-40 w-full resize-y`}
                    placeholder="Paste emails here, one per line or CSV first column"
                    value={recipientInput}
                    onChange={(e) => { setRecipientInput(e.target.value); setPreview(null); setPreviewKey(''); }}
                  />
                )}
              </div>

              {mode === 'batch' && (
                <div>
                  <label className="mb-2 block text-xs uppercase tracking-wide text-zinc-500">Upload CSV</label>
                  <div className="flex items-center gap-2">
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".csv,text/csv"
                      className="hidden"
                      onChange={(e) => { void uploadCsv(e.target.files?.[0] ?? null); }}
                    />
                    <button
                      type="button"
                      className={BTN_OUTLINE}
                      onClick={() => fileInputRef.current?.click()}
                    >
                      <Upload className="h-4 w-4" /> Choose CSV
                    </button>
                    <span className="text-xs text-zinc-500">First column or `email` header.</span>
                  </div>
                </div>
              )}

              <div className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 text-xs text-zinc-400">
                <div className="flex items-center gap-2 text-zinc-200">
                  <Sparkles className="h-4 w-4 text-fuchsia-300" />
                  Flow
                </div>
                <ol className="mt-2 space-y-1.5">
                  <li>1. Preview with dry-run.</li>
                  <li>2. Review the rendered email and mapping.</li>
                  <li>3. Confirm the real send.</li>
                </ol>
              </div>

              {error && (
                <div className="rounded-xl border border-red-900/70 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                  {error}
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className={BTN_PRIMARY}
                  onClick={() => run(true)}
                  disabled={busy}
                >
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />}
                  Preview
                </button>
                <button
                  type="button"
                  className={BTN_OUTLINE}
                  onClick={confirmSend}
                  disabled={busy || !previewMatches || !preview}
                >
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                  Send
                </button>
              </div>

              {preview && !previewMatches && (
                <div className="rounded-xl border border-amber-900/70 bg-amber-950/40 px-3 py-2 text-sm text-amber-300">
                  Preview is stale. Re-run Preview before sending.
                </div>
              )}
            </div>
          </div>

          <div className="max-h-[80vh] overflow-y-auto p-5">
            {!preview ? (
              <div className="rounded-2xl border border-dashed border-zinc-800 p-6 text-sm text-zinc-500">
                Dry-run output will show here.
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-400">
                  <span className="rounded-full bg-zinc-800 px-2.5 py-1">{preview.total} total</span>
                  <span className="rounded-full bg-emerald-500/15 px-2.5 py-1 text-emerald-300">{preview.sent} sent</span>
                  <span className="rounded-full bg-zinc-700/60 px-2.5 py-1 text-zinc-300">{preview.skipped} skipped</span>
                  <span className="rounded-full bg-red-500/15 px-2.5 py-1 text-red-300">{preview.failed} failed</span>
                </div>
                {preview.results.map((result) => (
                  <div key={`${result.code}:${result.email}`} className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-sm text-zinc-100">{result.code} → {result.email}</div>
                        <div className="text-xs text-zinc-500">
                          {result.emailed_at ? `emailed ${relative(result.emailed_at)}` : result.status}
                        </div>
                      </div>
                      <StatusPill status={result.status} />
                    </div>
                    {result.subject && (
                      <div className="mt-3 text-sm text-zinc-200">
                        <span className="text-xs uppercase tracking-wide text-zinc-500">Subject</span>
                        <div className="mt-1">{result.subject}</div>
                      </div>
                    )}
                    {result.text_body && (
                      <div className="mt-3">
                        <div className="text-xs uppercase tracking-wide text-zinc-500">Plain text</div>
                        <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded-xl border border-zinc-800 bg-zinc-950/80 p-3 text-xs leading-6 text-zinc-300">
                          {result.text_body}
                        </pre>
                      </div>
                    )}
                    {result.html_body && (
                      <details className="mt-3">
                        <summary className="cursor-pointer text-xs uppercase tracking-wide text-zinc-500">HTML source</summary>
                        <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap rounded-xl border border-zinc-800 bg-zinc-950/80 p-3 text-xs leading-6 text-zinc-400">
                          {result.html_body}
                        </pre>
                      </details>
                    )}
                    {result.error && (
                      <div className="mt-3 rounded-xl border border-red-900/70 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                        {result.error}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export default function CompsAdmin() {
  const { user } = useAuth();

  const [summary, setSummary] = useState<CompsSummary | null>(null);
  const [comps, setComps] = useState<CompUser[]>([]);
  const [codes, setCodes] = useState<InviteCodeAdminItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [compStatus, setCompStatus] = useState<'' | CompStatus>('');
  const [compQuery, setCompQuery] = useState('');
  const [codeFilter, setCodeFilter] = useState<'all' | 'available' | 'redeemed'>('all');
  const [cohortFilter, setCohortFilter] = useState('');

  // Grant form
  const [grantEmail, setGrantEmail] = useState('');
  const [grantTier, setGrantTier] = useState('pro');
  const [grantDays, setGrantDays] = useState(30);
  const [grantFounding, setGrantFounding] = useState(false);
  const [grantBusy, setGrantBusy] = useState(false);

  // Mint form
  const [mintCount, setMintCount] = useState(25);
  const [mintNote, setMintNote] = useState('');
  const [mintBusy, setMintBusy] = useState(false);
  const [emailModalOpen, setEmailModalOpen] = useState(false);
  const [emailMode, setEmailMode] = useState<InviteEmailMode>('single');
  const [emailCode, setEmailCode] = useState<string | null>(null);
  const [emailCohort, setEmailCohort] = useState<string | null>(null);

  const isSuper = Boolean(user?.is_superuser);

  const loadAll = async () => {
    setError(null);
    try {
      const [s, c, k] = await Promise.all([
        apiGet<CompsSummary>('/api/admin/comps/summary'),
        apiGet<CompUser[]>('/api/admin/comps'),
        apiGet<InviteCodeAdminItem[]>('/api/admin/invite-codes'),
      ]);
      setSummary(s);
      setComps(c);
      setCodes(k);
    } catch (err) {
      setError(`Failed to load launch data: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isSuper) loadAll();
    else setLoading(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuper]);

  // Client-side filtering (server returns the full set; these are cheap).
  const filteredComps = useMemo(() => {
    return comps.filter((u) => {
      if (compStatus && u.status !== compStatus) return false;
      if (compQuery) {
        const q = compQuery.trim().toLowerCase();
        if (!u.email.toLowerCase().includes(q) && !u.username.toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [comps, compStatus, compQuery]);

  const cohorts = useMemo(() => {
    const set = new Set<string>();
    codes.forEach((c) => c.cohort && set.add(c.cohort));
    return Array.from(set).sort();
  }, [codes]);

  const filteredCodes = useMemo(() => {
    return codes.filter((c) => {
      if (codeFilter === 'available' && (c.redeemed || !c.is_active)) return false;
      if (codeFilter === 'redeemed' && !c.redeemed) return false;
      if (cohortFilter && c.cohort !== cohortFilter) return false;
      return true;
    });
  }, [codes, codeFilter, cohortFilter]);

  const grant = async (revoke: boolean, emailOverride?: string) => {
    const email = (emailOverride ?? grantEmail).trim();
    if (!email) {
      showToast.error('Enter an email.');
      return;
    }
    if (revoke) {
      const ok = await showConfirm(
        `Revoke the comp for ${email}? Their tier drops to free immediately.`,
        { confirmLabel: 'Revoke', tone: 'danger' },
      );
      if (!ok) return;
    }
    setGrantBusy(true);
    try {
      const path = revoke ? '/api/admin/comps/revoke' : '/api/admin/comps/grant';
      const body = revoke
        ? { email }
        : { email, tier: grantTier, days: grantDays, founding: grantFounding, cohort: 'meeting_ops_v1' };
      const res = await apiPost<CompActionResponse>(path, body);
      showToast.success(
        revoke
          ? `Revoked ${res.after.email} → free.`
          : `${res.after.email} → ${res.after.tier} (org ${res.after.personal_org_plan}).`,
      );
      if (!revoke && !emailOverride) setGrantEmail('');
      await loadAll();
    } catch (err) {
      showToast.error(`${revoke ? 'Revoke' : 'Grant'} failed: ${(err as Error).message}`);
    } finally {
      setGrantBusy(false);
    }
  };

  const mint = async () => {
    if (mintCount < 1) return;
    setMintBusy(true);
    try {
      const minted = await apiPost<{ code: string }[]>('/api/admin/invite-codes', {
        count: Math.min(mintCount, 50),
        note: mintNote.trim() || 'cohort=meeting_ops_v1 comp_days=30',
      });
      showToast.success(`Minted ${minted.length} code${minted.length === 1 ? '' : 's'}.`);
      setMintNote('');
      await loadAll();
    } catch (err) {
      showToast.error(`Mint failed: ${(err as Error).message}`);
    } finally {
      setMintBusy(false);
    }
  };

  const copyAvailable = async () => {
    const list = filteredCodes.filter((c) => c.is_active && !c.redeemed).map((c) => c.code);
    if (!list.length) {
      showToast.info('No available codes in the current filter.');
      return;
    }
    await copy(list.join('\n'), `Copied ${list.length} available codes.`);
  };

  const exportCsv = () => {
    const header = ['code', 'status', 'cohort', 'redeemed_by_email', 'redeemed_at', 'created_at', 'note'];
    const esc = (v: unknown) => {
      const s = v == null ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const rows = filteredCodes.map((c) => [
      c.code,
      c.redeemed ? 'redeemed' : c.is_active ? 'available' : 'inactive',
      c.cohort ?? '',
      c.redeemed_by_email ?? '',
      c.redeemed_at ?? '',
      c.created_at ?? '',
      c.note ?? '',
    ].map(esc).join(','));
    const blob = new Blob([[header.join(','), ...rows].join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `invite-codes-${cohortFilter || 'all'}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const openSingleEmail = (code: string, cohort?: string | null) => {
    setEmailMode('single');
    setEmailCode(code);
    setEmailCohort(cohort ?? null);
    setEmailModalOpen(true);
  };

  const openBatchEmail = () => {
    setEmailMode('batch');
    setEmailCode(null);
    setEmailCohort(cohortFilter || cohorts[0] || null);
    setEmailModalOpen(true);
  };

  // --- Non-superuser: access notice (AdminRoute lets org-admins reach here) ---
  if (!isSuper) {
    return (
      <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black p-6 text-white">
        <div className="mx-auto max-w-lg">
          <div className={`${CARD} flex items-start gap-3`}>
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-amber-400" />
            <div>
              <div className="font-medium">Platform superuser only</div>
              <p className="mt-1 text-sm text-zinc-400">
                The launch console manages comps and invite codes across the whole platform.
                It's restricted to a platform superuser.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-zinc-950 to-black p-6 text-white">
      <div className="mx-auto max-w-7xl">
        {/* Header */}
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-purple-600 to-indigo-600">
              <Rocket className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-xl font-semibold">Launch Console</h1>
              <p className="text-sm text-zinc-500">Comps &amp; invite codes for the launch cohort</p>
            </div>
          </div>
          <button onClick={loadAll} className={BTN_OUTLINE} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} /> Refresh
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {/* Summary cards */}
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <SummaryCard icon={<CircleCheck className="h-5 w-5 text-emerald-300" />} tone="bg-emerald-500/15"
            label="Active comps" value={summary?.active_comps ?? '—'} />
          <SummaryCard icon={<Clock className="h-5 w-5 text-red-300" />} tone="bg-red-500/15"
            label="Expired" value={summary?.expired_pending_revert ?? '—'} />
          <SummaryCard icon={<CreditCard className="h-5 w-5 text-sky-300" />} tone="bg-sky-500/15"
            label="Permanent / paid" value={summary?.permanent ?? '—'} />
          <SummaryCard icon={<Ticket className="h-5 w-5 text-emerald-300" />} tone="bg-emerald-500/15"
            label="Codes available" value={summary?.codes_available ?? '—'} />
          <SummaryCard icon={<Gift className="h-5 w-5 text-fuchsia-300" />} tone="bg-fuchsia-500/15"
            label="Codes redeemed" value={summary?.codes_redeemed ?? '—'} />
          <SummaryCard icon={<Crown className="h-5 w-5 text-purple-300" />} tone="bg-purple-500/15"
            label="Superusers" value={summary?.superusers ?? '—'} />
        </div>

        {/* Action cards: grant + mint */}
        <div className="mb-6 grid gap-4 lg:grid-cols-2">
          {/* Grant / extend a comp */}
          <div className={CARD}>
            <div className="mb-3 flex items-center gap-2 text-sm font-medium">
              <UserPlus className="h-4 w-4 text-indigo-300" /> Grant / extend a comp
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <input
                className={`${INPUT} min-w-[200px] flex-1`}
                placeholder="user@email.com"
                value={grantEmail}
                onChange={(e) => setGrantEmail(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !grantBusy) grant(false); }}
              />
              <select className={INPUT} value={grantTier} onChange={(e) => setGrantTier(e.target.value)}>
                <option value="pro">pro</option>
                <option value="basic">basic</option>
                <option value="suite">suite</option>
                <option value="enterprise">enterprise</option>
              </select>
              <input
                type="number" min={1} max={3650}
                className={`${INPUT} w-24`}
                value={grantDays}
                onChange={(e) => setGrantDays(Math.max(1, Number(e.target.value) || 1))}
                title="days"
              />
              <label className="flex items-center gap-1.5 text-xs text-zinc-400">
                <input type="checkbox" checked={grantFounding} onChange={(e) => setGrantFounding(e.target.checked)} />
                Founding
              </label>
              <button className={BTN_PRIMARY} disabled={grantBusy} onClick={() => grant(false)}>
                <UserPlus className="h-4 w-4" /> Grant
              </button>
            </div>
            <p className="mt-2 text-xs text-zinc-500">
              Sets user tier + a {grantDays}-day expiry AND the personal org plan. Re-granting extends the window.
              Comps auto-revert to free at expiry unless the user subscribes.
            </p>
          </div>

          {/* Mint invite codes */}
          <div className={CARD}>
            <div className="mb-3 flex items-center gap-2 text-sm font-medium">
              <Plus className="h-4 w-4 text-emerald-300" /> Mint invite codes
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <input
                type="number" min={1} max={50}
                className={`${INPUT} w-24`}
                value={mintCount}
                onChange={(e) => setMintCount(Math.min(50, Math.max(1, Number(e.target.value) || 1)))}
                title="count (max 50 per mint)"
              />
              <input
                className={`${INPUT} min-w-[180px] flex-1`}
                placeholder="note (default cohort=meeting_ops_v1 comp_days=30)"
                value={mintNote}
                onChange={(e) => setMintNote(e.target.value)}
              />
              <button className={BTN_PRIMARY} disabled={mintBusy} onClick={mint}>
                <Ticket className="h-4 w-4" /> Mint
              </button>
            </div>
            <p className="mt-2 text-xs text-zinc-500">
              Single-use codes → a 30-day Pro comp on redemption (no card). Max 50 per mint.
            </p>
          </div>
        </div>

        {/* Comped users table */}
        <div className="mb-6">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
              Comped &amp; paid users {summary ? `(${summary.total_nonfree})` : ''}
            </h2>
            <div className="flex items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                <input
                  className={`${INPUT} w-48 pl-8`}
                  placeholder="search email…"
                  value={compQuery}
                  onChange={(e) => setCompQuery(e.target.value)}
                />
              </div>
              <select className={INPUT} value={compStatus} onChange={(e) => setCompStatus(e.target.value as '' | CompStatus)}>
                <option value="">all statuses</option>
                <option value="active">active</option>
                <option value="expired">expired</option>
                <option value="permanent">permanent / paid</option>
                <option value="superuser">superuser</option>
              </select>
            </div>
          </div>

          <div className="overflow-hidden rounded-lg border border-zinc-800">
            <div className="grid grid-cols-12 gap-3 border-b border-zinc-800 bg-zinc-950/70 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500">
              <div className="col-span-4">User</div>
              <div className="col-span-2">Tier</div>
              <div className="col-span-2">Status</div>
              <div className="col-span-2">Expires</div>
              <div className="col-span-2 text-right">Actions</div>
            </div>
            {loading ? (
              <div className="flex items-center gap-2 px-4 py-6 text-sm text-zinc-400">
                <RefreshCw className="h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : filteredComps.length === 0 ? (
              <div className="px-4 py-8 text-sm text-zinc-500">No matching users.</div>
            ) : (
              filteredComps.map((u) => (
                <div key={u.id} className="grid grid-cols-12 items-center gap-3 border-b border-zinc-800 px-4 py-3 text-sm last:border-b-0">
                  <div className="col-span-4 min-w-0">
                    <div className="truncate font-medium">{u.email}</div>
                    <div className="truncate text-xs text-zinc-500">
                      @{u.username}
                      {u.is_founding_member && <span className="ml-1 text-amber-400">· founding</span>}
                      {u.has_stripe && <span className="ml-1 text-sky-400">· stripe</span>}
                    </div>
                  </div>
                  <div className="col-span-2">
                    <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs">{u.tier}</span>
                    {u.personal_org_plan && u.personal_org_plan !== u.tier && (
                      <span className="ml-1 text-xs text-amber-400" title="org plan differs from tier">
                        org:{u.personal_org_plan}
                      </span>
                    )}
                  </div>
                  <div className="col-span-2">
                    <CompStatusBadge status={u.status} days={u.days_remaining} />
                  </div>
                  <div className="col-span-2 text-xs text-zinc-400">{relative(u.tier_expires_at)}</div>
                  <div className="col-span-2 flex justify-end gap-1">
                    {u.status !== 'superuser' && (
                      <button
                        className="inline-flex items-center gap-1 rounded border border-red-900/70 px-2 py-1 text-xs text-red-300 hover:bg-red-950/50 disabled:opacity-50"
                        disabled={grantBusy}
                        onClick={() => grant(true, u.email)}
                        title="Revoke comp → free"
                      >
                        <Ban className="h-3.5 w-3.5" /> Revoke
                      </button>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Invite codes table */}
        <div>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
              Invite codes {summary ? `(${summary.codes_available} available / ${summary.codes_total})` : ''}
            </h2>
            <div className="flex flex-wrap items-center gap-2">
              <select className={INPUT} value={cohortFilter} onChange={(e) => setCohortFilter(e.target.value)}>
                <option value="">all cohorts</option>
                {cohorts.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select className={INPUT} value={codeFilter} onChange={(e) => setCodeFilter(e.target.value as 'all' | 'available' | 'redeemed')}>
                <option value="all">all</option>
                <option value="available">available</option>
                <option value="redeemed">redeemed</option>
              </select>
              <button className={BTN_OUTLINE} onClick={copyAvailable} title="Copy available codes">
                <Copy className="h-4 w-4" /> Copy available
              </button>
              <button className={BTN_OUTLINE} onClick={openBatchEmail} title="Preview and email codes from a cohort">
                <Mail className="h-4 w-4" /> Email codes…
              </button>
              <button className={BTN_OUTLINE} onClick={exportCsv} title="Export filtered as CSV">
                <Download className="h-4 w-4" /> CSV
              </button>
            </div>
          </div>

          <div className="overflow-hidden rounded-lg border border-zinc-800">
            <div className="grid grid-cols-12 gap-3 border-b border-zinc-800 bg-zinc-950/70 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500">
              <div className="col-span-3">Code</div>
              <div className="col-span-2">Status</div>
              <div className="col-span-2">Cohort</div>
              <div className="col-span-3">Redeemed by</div>
              <div className="col-span-2 text-right">Actions</div>
            </div>
            {loading ? (
              <div className="flex items-center gap-2 px-4 py-6 text-sm text-zinc-400">
                <RefreshCw className="h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : filteredCodes.length === 0 ? (
              <div className="px-4 py-8 text-sm text-zinc-500">No codes match the filter.</div>
            ) : (
              filteredCodes.slice(0, 500).map((c) => (
                <div key={c.code} className="grid grid-cols-12 items-center gap-3 border-b border-zinc-800 px-4 py-3 text-sm last:border-b-0">
                  <div className="col-span-3 font-mono text-sm">{c.code}</div>
                  <div className="col-span-2"><CodeStatusBadge c={c} /></div>
                  <div className="col-span-2 truncate text-xs text-zinc-400">{c.cohort ?? '—'}</div>
                  <div className="col-span-3 min-w-0">
                    {c.redeemed_by_email ? (
                      <div className="truncate">
                        <span className="truncate">{c.redeemed_by_email}</span>
                        <span className="ml-1 text-xs text-zinc-500">{relative(c.redeemed_at)}</span>
                      </div>
                    ) : (
                      <span className="text-xs text-zinc-600">—</span>
                    )}
                  </div>
                  <div className="col-span-2 flex justify-end">
                    <div className="flex items-center gap-1">
                      <button
                        className="rounded p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
                        onClick={() => openSingleEmail(c.code, c.cohort)}
                        title={c.emailed_at ? 'Already emailed' : 'Email this code'}
                        disabled={Boolean(c.emailed_at) || c.redeemed}
                      >
                        <Mail className="h-4 w-4" />
                      </button>
                      <button
                        className="rounded p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
                        onClick={() => copy(c.code, `Copied ${c.code}.`)}
                        title="Copy code"
                      >
                        <Copy className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
          {filteredCodes.length > 500 && (
            <p className="mt-2 text-xs text-zinc-500">
              Showing first 500 of {filteredCodes.length}. Narrow with a cohort/status filter or export CSV.
            </p>
          )}
        </div>
      </div>

      <InviteCodeEmailModal
        open={emailModalOpen}
        mode={emailMode}
        initialCode={emailCode}
        initialCohort={emailCohort}
        availableCohorts={cohorts}
        onClose={() => setEmailModalOpen(false)}
        onRefresh={loadAll}
      />
    </div>
  );
}
