/**
 * Workspace → Integrations settings panel (v3.19.0).
 *
 * Lists the 5 per-org integrations (Brigade, Project-Ops, Contact-Ops,
 * Accounting-Ops, Stable) as enable/disable cards. Each card exposes:
 *
 *   - Enable / Disable toggle
 *   - API base URL input
 *   - API key input (password-masked, show toggle, last 4 hint when set)
 *   - Integration-specific extras (Brigade tenancy mode + shared agent;
 *     Project-Ops default project number)
 *   - Test connection button (best-effort upstream probe)
 *   - Save button (per card)
 *   - Disable + clear creds button (DELETE)
 *
 * Auth: GET visible to any org member; PUT/DELETE/test gated server-side
 * on org admin. The UI mirrors that by rendering read-only inputs +
 * hiding action buttons for non-admins, with an "Ask your org admin"
 * hint at the top.
 *
 * Secret handling: this component NEVER displays the cleartext API key.
 * The "show" toggle only reveals what the admin is currently typing in
 * the input field — it cannot reveal the stored key. The masked
 * "•••• xxxx" hint comes from the server's api_key_last4 + has_api_key.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  CheckCircle2,
  Eye,
  EyeOff,
  Network,
  Loader2,
  Trash2,
  XCircle,
  Calendar,
} from 'lucide-react';
import { config } from '../../config';
import { showToast } from '../Toast';
import { useAuth } from '../../contexts/AuthContext';
import {
  disableIntegration,
  listIntegrations,
  testIntegration,
  updateIntegration,
  type IntegrationKey,
  type IntegrationUpdatePayload,
  type IntegrationView,
} from '../../services/integrationsApi';

interface CardDef {
  key: IntegrationKey;
  title: string;
  description: string;
}

const CARDS: CardDef[] = [
  {
    key: 'brigade',
    title: 'Brigade',
    description:
      'Knowledge-graph writes for completed meetings (entities, action items, decisions).',
  },
  {
    key: 'project_ops',
    title: 'Project-Ops',
    description: 'Send meeting action items to the Project-Ops triage inbox for review.',
  },
  {
    key: 'contact_ops',
    title: 'Contact-Ops',
    description: 'Sync meeting participants to your contacts database.',
  },
  {
    key: 'accounting_ops',
    title: 'Accounting-Ops',
    description: 'Push billable-hour and client-meeting metadata.',
  },
  {
    key: 'stable',
    title: 'Stable',
    description: 'Post meeting summaries into your Stable threads.',
  },
  {
    key: 'jira',
    title: 'Jira',
    description: 'Push action items to Jira as issues. API key format: your-email@company.com:api-token (Atlassian Cloud Basic auth).',
  },
  {
    key: 'todoist',
    title: 'Todoist',
    description: 'Push action items to Todoist as tasks.',
  },
  {
    key: 'clickup',
    title: 'ClickUp',
    description: 'Push action items to a ClickUp list as tasks.',
  },
  {
    key: 'notion',
    title: 'Notion',
    description: 'Export meeting summaries and action items to a Notion database.',
  },
  {
    key: 'confluence',
    title: 'Confluence',
    description: 'Publish meeting/lecture insights as Confluence pages. Uses the same email:token format as Jira.',
  },
  {
    key: 'slack',
    title: 'Slack',
    description: 'Send action-item notifications via a Slack Incoming Webhook URL (paste the webhook URL as the API base URL; no API key needed).',
  },
  {
    key: 'teams',
    title: 'Microsoft Teams',
    description: 'Send action-item notifications via a Teams Incoming Webhook (connector) URL.',
  },
  {
    key: 'email',
    title: 'Email',
    description: 'Send action-item/decision notifications via SMTP. Configure host/port/username in the extra fields; the account password goes in the API key field.',
  },
];

const TENANCY_MODES: Array<{ value: string; label: string }> = [
  { value: 'shared', label: 'Shared (one graph across all orgs)' },
  { value: 'per_org_graph', label: 'Per-org graph (isolated)' },
  { value: 'per_org_instance', label: 'Per-org Brigade instance' },
];

interface FormState {
  enabled: boolean;
  api_base_url: string;
  /** Empty string = no edit pending; user types here to update creds. */
  api_key: string;
  tenancy_mode: string;
  shared_agent_id: string;
  default_project_number: string;
  auto_push_action_items: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_from_address: string;
  smtp_use_tls: boolean;
  showKey: boolean;
}

interface GoogleCalendarStatus {
  configured: boolean;
  connected: boolean;
  google_email?: string | null;
  status: string;
  connected_at?: string | null;
  last_sync_error?: string | null;
}

function GoogleCalendarCard({ isAdmin }: { isAdmin: boolean }) {
  const [status, setStatus] = useState<GoogleCalendarStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const base = `${config.apiBaseUrl}/api/integrations/google-calendar`;

  const load = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${base}/status`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStatus(await res.json());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${base}/authorize`);
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      const { authorization_url } = await res.json();
      // Full-page redirect to Google's consent screen — the callback
      // route completes the flow and the user lands back in-app.
      window.location.href = authorization_url;
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${base}/disconnect`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showToast.success('Google Calendar disconnected');
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Calendar className="w-4 h-4 text-purple-400" />
          <h3 className="text-sm font-medium text-white">Google Calendar</h3>
        </div>
        {loading ? (
          <Loader2 className="w-4 h-4 animate-spin text-zinc-500" />
        ) : status?.connected ? (
          <span className="inline-flex items-center gap-1 text-xs text-emerald-400">
            <CheckCircle2 className="w-3.5 h-3.5" /> Connected{status.google_email ? ` as ${status.google_email}` : ''}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-xs text-zinc-500">
            <XCircle className="w-3.5 h-3.5" /> Not connected
          </span>
        )}
      </div>
      <p className="text-xs text-zinc-500">
        Create Google Calendar events directly from confirmed action items — one click per item, nothing automatic.
      </p>
      {!loading && status && !status.configured && (
        <p className="text-xs text-amber-400">
          Not available on this deployment — the server admin needs to set GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI.
        </p>
      )}
      {error && <p className="text-xs text-red-400">{error}</p>}
      {isAdmin && !loading && status?.configured && (
        <button
          type="button"
          onClick={status.connected ? disconnect : connect}
          disabled={busy}
          className="inline-flex items-center gap-2 rounded-md bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 px-3 py-1.5 text-xs font-medium text-white"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
          {status.connected ? 'Disconnect' : 'Connect Google Calendar'}
        </button>
      )}
    </div>
  );
}

function defaultFormFromView(view: IntegrationView): FormState {
  const extra = (view.extra || {}) as Record<string, unknown>;
  return {
    enabled: view.enabled,
    api_base_url: view.api_base_url || '',
    api_key: '',
    tenancy_mode: (extra.tenancy_mode as string) || 'shared',
    shared_agent_id: (extra.shared_agent_id as string) || '',
    default_project_number: (extra.default_project_number as string) || '',
    auto_push_action_items: Boolean(extra.auto_push_action_items),
    smtp_host: (extra.smtp_host as string) || '',
    smtp_port: (extra.smtp_port as number) || 587,
    smtp_username: (extra.smtp_username as string) || '',
    smtp_from_address: (extra.smtp_from_address as string) || '',
    smtp_use_tls: extra.smtp_use_tls !== undefined ? Boolean(extra.smtp_use_tls) : true,
    showKey: false,
  };
}

export default function IntegrationsPanel() {
  const { user } = useAuth();
  const isAdmin = useMemo(() => {
    if (!user) return false;
    if (user.is_superuser) return true;
    return user.active_organization?.role === 'admin';
  }, [user]);

  const [views, setViews] = useState<Record<string, IntegrationView>>({});
  const [forms, setForms] = useState<Record<string, FormState>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<IntegrationKey | null>(null);
  const [testResult, setTestResult] = useState<
    Record<string, { ok: boolean; message: string } | null>
  >({});

  // ------------------------------------------------------------------
  // Load
  // ------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const rows = await listIntegrations();
        if (cancelled) return;
        const byKey: Record<string, IntegrationView> = {};
        const formByKey: Record<string, FormState> = {};
        for (const row of rows) {
          byKey[row.key] = row;
          formByKey[row.key] = defaultFormFromView(row);
        }
        setViews(byKey);
        setForms(formByKey);
      } catch (err) {
        if (!cancelled) {
          setError((err as Error).message || 'Failed to load integrations');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const updateForm = (key: IntegrationKey, patch: Partial<FormState>) => {
    setForms((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }));
  };

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  const handleSave = async (key: IntegrationKey) => {
    const form = forms[key];
    if (!form) return;
    setBusyKey(key);
    setTestResult((prev) => ({ ...prev, [key]: null }));
    try {
      const payload: IntegrationUpdatePayload = {
        enabled: form.enabled,
        api_base_url: form.api_base_url,
      };
      // Only send api_key when the admin actually typed something.
      // Empty string means "leave existing alone" from the UI POV;
      // explicit clear is the Disable + clear button.
      if (form.api_key && form.api_key.length > 0) {
        payload.api_key = form.api_key;
      }
      if (key === 'brigade') {
        payload.tenancy_mode = form.tenancy_mode as IntegrationUpdatePayload['tenancy_mode'];
        payload.shared_agent_id = form.shared_agent_id;
      } else if (key === 'project_ops') {
        payload.default_project_number = form.default_project_number;
        payload.auto_push_action_items = form.auto_push_action_items;
      } else if (key === 'email') {
        payload.smtp_host = form.smtp_host;
        payload.smtp_port = form.smtp_port;
        payload.smtp_username = form.smtp_username;
        payload.smtp_from_address = form.smtp_from_address;
        payload.smtp_use_tls = form.smtp_use_tls;
      }
      const next = await updateIntegration(key, payload);
      setViews((prev) => ({ ...prev, [key]: next }));
      // Wipe the api_key field after save — never keep cleartext in memory
      // longer than needed.
      setForms((prev) => ({
        ...prev,
        [key]: { ...defaultFormFromView(next) },
      }));
      showToast.success(`${labelFor(key)} saved`);
    } catch (err) {
      showToast.error(`Save failed: ${(err as Error).message}`);
    } finally {
      setBusyKey(null);
    }
  };

  const handleDisable = async (key: IntegrationKey) => {
    setBusyKey(key);
    setTestResult((prev) => ({ ...prev, [key]: null }));
    try {
      const next = await disableIntegration(key);
      setViews((prev) => ({ ...prev, [key]: next }));
      setForms((prev) => ({ ...prev, [key]: defaultFormFromView(next) }));
      showToast.success(`${labelFor(key)} disabled`);
    } catch (err) {
      showToast.error(`Disable failed: ${(err as Error).message}`);
    } finally {
      setBusyKey(null);
    }
  };

  const handleTest = async (key: IntegrationKey) => {
    setBusyKey(key);
    setTestResult((prev) => ({ ...prev, [key]: null }));
    try {
      const result = await testIntegration(key);
      setTestResult((prev) => ({
        ...prev,
        [key]: {
          ok: result.ok,
          message: result.ok ? 'Connection ok' : result.error || 'Failed',
        },
      }));
    } catch (err) {
      setTestResult((prev) => ({
        ...prev,
        [key]: { ok: false, message: (err as Error).message },
      }));
    } finally {
      setBusyKey(null);
    }
  };

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-zinc-400 p-4">
        <Loader2 className="w-4 h-4 animate-spin" />
        Loading integrations…
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-md border border-red-700 bg-red-900/20 text-red-200 p-4">
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-white">Workspace integrations</h2>
        <p className="text-sm text-zinc-400 mt-1">
          Per-org credentials for connected apps. Each integration is opt-in
          — credentials live only on this workspace and never leak across
          customers.
        </p>
        {!isAdmin && (
          <div className="mt-3 rounded-md border border-amber-700/60 bg-amber-900/20 text-amber-200 text-sm px-3 py-2">
            You're viewing this in read-only mode. Ask your org admin to
            change integration settings.
          </div>
        )}
      </div>

      <GoogleCalendarCard isAdmin={isAdmin} />

      {CARDS.map((card) => {
        const view = views[card.key];
        const form = forms[card.key];
        if (!view || !form) return null;
        const test = testResult[card.key];
        const busy = busyKey === card.key;
        return (
          <div
            key={card.key}
            className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-4 sm:p-5"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-start gap-3">
                <Network className="w-5 h-5 text-purple-400 mt-0.5" />
                <div>
                  <h3 className="text-white font-medium">{card.title}</h3>
                  <p className="text-sm text-zinc-400">{card.description}</p>
                </div>
              </div>
              <ToggleSwitch
                checked={form.enabled}
                onChange={(v) => updateForm(card.key, { enabled: v })}
                disabled={!isAdmin || busy}
                ariaLabel={`Enable ${card.title}`}
              />
            </div>

            {form.enabled && (
              <div className="mt-4 space-y-3">
                <Field
                  label="API base URL"
                  value={form.api_base_url}
                  onChange={(v) => updateForm(card.key, { api_base_url: v })}
                  placeholder="https://example.local"
                  disabled={!isAdmin || busy}
                />

                <div>
                  <label className="block text-xs font-medium text-zinc-300 mb-1">
                    API key
                    {view.has_api_key && (
                      <span className="ml-2 text-zinc-500">
                        (stored: ••••{view.api_key_last4 || '????'})
                      </span>
                    )}
                  </label>
                  <div className="flex gap-2">
                    <input
                      type={form.showKey ? 'text' : 'password'}
                      className="flex-1 rounded-md bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-purple-500"
                      placeholder={
                        view.has_api_key
                          ? 'Leave blank to keep current key'
                          : 'Paste API key'
                      }
                      value={form.api_key}
                      onChange={(e) =>
                        updateForm(card.key, { api_key: e.target.value })
                      }
                      disabled={!isAdmin || busy}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      className="px-3 py-2 rounded-md bg-zinc-800 hover:bg-zinc-700 text-zinc-200"
                      onClick={() =>
                        updateForm(card.key, { showKey: !form.showKey })
                      }
                      disabled={!isAdmin || busy}
                      title={form.showKey ? 'Hide' : 'Show'}
                    >
                      {form.showKey ? (
                        <EyeOff className="w-4 h-4" />
                      ) : (
                        <Eye className="w-4 h-4" />
                      )}
                    </button>
                  </div>
                </div>

                {card.key === 'brigade' && (
                  <>
                    <div>
                      <label className="block text-xs font-medium text-zinc-300 mb-1">
                        Tenancy mode
                      </label>
                      <select
                        className="w-full rounded-md bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-purple-500"
                        value={form.tenancy_mode}
                        onChange={(e) =>
                          updateForm(card.key, { tenancy_mode: e.target.value })
                        }
                        disabled={!isAdmin || busy}
                      >
                        {TENANCY_MODES.map((m) => (
                          <option key={m.value} value={m.value}>
                            {m.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    {form.tenancy_mode === 'shared' && (
                      <Field
                        label="Shared agent ID (optional)"
                        value={form.shared_agent_id}
                        onChange={(v) =>
                          updateForm(card.key, { shared_agent_id: v })
                        }
                        placeholder="meeting_ops_canonical"
                        disabled={!isAdmin || busy}
                      />
                    )}
                  </>
                )}

                {card.key === 'project_ops' && (
                  <>
                    <Field
                      label="Default project number (optional)"
                      value={form.default_project_number}
                      onChange={(v) =>
                        updateForm(card.key, { default_project_number: v })
                      }
                      placeholder="P-00055"
                      disabled={!isAdmin || busy}
                    />
                    <div className="flex items-start justify-between gap-3 pt-1">
                      <div className="pr-2">
                        <label className="block text-xs font-medium text-zinc-300">
                          Auto-send action items to triage
                        </label>
                        <p className="text-xs text-zinc-500 mt-1">
                          When on, action items from completed meetings are sent
                          to the Project-Ops triage inbox for review. Propose-only
                          — the triage agent routes each item and nothing becomes
                          a task until someone approves it in Project-Ops. Off by
                          default.
                        </p>
                      </div>
                      <ToggleSwitch
                        checked={form.auto_push_action_items}
                        onChange={(v) =>
                          updateForm(card.key, { auto_push_action_items: v })
                        }
                        disabled={!isAdmin || busy}
                        ariaLabel="Auto-send action items to the Project-Ops triage inbox"
                      />
                    </div>
                  </>
                )}

                {card.key === 'email' && (
                  <>
                    <Field
                      label="SMTP host"
                      value={form.smtp_host}
                      onChange={(v) => updateForm(card.key, { smtp_host: v })}
                      placeholder="smtp.yourcompany.com"
                      disabled={!isAdmin || busy}
                    />
                    <Field
                      label="SMTP port"
                      value={String(form.smtp_port)}
                      onChange={(v) => updateForm(card.key, { smtp_port: Number(v) || 587 })}
                      placeholder="587"
                      disabled={!isAdmin || busy}
                    />
                    <Field
                      label="SMTP username"
                      value={form.smtp_username}
                      onChange={(v) => updateForm(card.key, { smtp_username: v })}
                      placeholder="notifier@yourcompany.com"
                      disabled={!isAdmin || busy}
                    />
                    <Field
                      label="From address"
                      value={form.smtp_from_address}
                      onChange={(v) => updateForm(card.key, { smtp_from_address: v })}
                      placeholder="notifications@yourcompany.com"
                      disabled={!isAdmin || busy}
                    />
                    <p className="text-xs text-zinc-500">
                      The SMTP account password goes in the API key field above — it's encrypted at rest the same as every other integration's credential.
                    </p>
                  </>
                )}
              </div>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-2">
              {isAdmin && (
                <>
                  <button
                    type="button"
                    onClick={() => void handleSave(card.key)}
                    disabled={busy}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-purple-600 hover:bg-purple-700 disabled:opacity-50 text-white text-sm font-medium"
                  >
                    {busy ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : null}
                    Save
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleTest(card.key)}
                    disabled={busy || !view.enabled || !view.has_api_key}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 text-sm"
                    title={
                      !view.enabled || !view.has_api_key
                        ? 'Save credentials first'
                        : 'Probe the upstream'
                    }
                  >
                    Test connection
                  </button>
                  {view.has_api_key && (
                    <button
                      type="button"
                      onClick={() => void handleDisable(card.key)}
                      disabled={busy}
                      className="inline-flex items-center gap-2 px-3 py-2 rounded-md text-red-300 hover:bg-red-900/30 disabled:opacity-50 text-sm"
                    >
                      <Trash2 className="w-4 h-4" />
                      Disable & clear key
                    </button>
                  )}
                </>
              )}
              {test && (
                <span
                  className={`inline-flex items-center gap-1 text-sm ml-1 ${
                    test.ok ? 'text-emerald-300' : 'text-red-300'
                  }`}
                >
                  {test.ok ? (
                    <CheckCircle2 className="w-4 h-4" />
                  ) : (
                    <XCircle className="w-4 h-4" />
                  )}
                  {test.message}
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Local primitives
// ---------------------------------------------------------------------------

function labelFor(key: IntegrationKey): string {
  return CARDS.find((c) => c.key === key)?.title || key;
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-zinc-300 mb-1">
        {label}
      </label>
      <input
        type="text"
        className="w-full rounded-md bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-purple-500"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      />
    </div>
  );
}

function ToggleSwitch({
  checked,
  onChange,
  disabled,
  ariaLabel,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors disabled:opacity-50 ${
        checked ? 'bg-purple-600' : 'bg-zinc-700'
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          checked ? 'translate-x-6' : 'translate-x-1'
        }`}
      />
    </button>
  );
}
