import React, { useEffect, useState } from 'react';
import { useOrg } from '../contexts/OrgContext';
import { config } from '../config';

interface ProviderConfig {
  service_kind: string;
  provider_name: string;
  endpoint_url?: string;
  api_key?: string;
  model_name?: string;
  overrides?: Record<string, any>;
}

const SERVICE_LABELS: Record<string, string> = {
  llm: 'LLM (Chat)',
  stt: 'Speech-to-Text',
  tts: 'Text-to-Speech',
  embeddings: 'Embeddings',
  reranking: 'Reranking',
  diarization: 'Diarization',
};

// Static fallback list — matches the canonical list in backend
// api/provider_settings.py. The component also fetches
// /api/providers/available/{kind} on mount so newly added backend
// providers surface in the dropdown without a frontend redeploy.
const VALID_PROVIDERS: Record<string, string[]> = {
  llm: ['litellm', 'openai', 'anthropic', 'openrouter', 'lambda', 'bedrock'],
  stt: ['parakeet', 'local_whisper', 'deepgram', 'assemblyai', 'openai'],
  tts: ['kokoro', 'vibevoice', 'openai'],
  embeddings: ['infinity', 'openai', 'voyage', 'cohere'],
  reranking: ['infinity', 'cohere', 'voyage'],
  diarization: ['local_diarization', 'off', 'deepgram', 'assemblyai'],
};

export default function ProviderSettingsPanel() {
  const { activeOrganization } = useOrg();
  const [editingKind, setEditingKind] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderConfig>({
    service_kind: '',
    provider_name: '',
    endpoint_url: '',
    api_key: '',
    model_name: '',
    overrides: {},
  });
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [providers, setProviders] = useState<Record<string, string[]>>(VALID_PROVIDERS);

  const getHeaders = () => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (activeOrganization?.slug) {
      headers['X-MeetingOps-Org'] = activeOrganization.slug;
    }
    return headers;
  };

  // Sync the dropdown list from the backend so newly registered providers
  // (like parakeet for STT) appear without a frontend redeploy.
  useEffect(() => {
    let cancelled = false;
    const kinds = Object.keys(VALID_PROVIDERS);
    const refresh = async () => {
      try {
        const updates = await Promise.all(
          kinds.map(async (kind) => {
            try {
              const res = await fetch(
                `${config.apiBaseUrl}/api/providers/available/${kind}`,
                { headers: getHeaders() }
              );
              if (res.ok) {
                const body = await res.json();
                if (Array.isArray(body?.providers) && body.providers.length > 0) {
                  return [kind, body.providers as string[]] as const;
                }
              }
            } catch {
              /* fall through to static */
            }
            return [kind, VALID_PROVIDERS[kind]] as const;
          })
        );
        if (cancelled) return;
        setProviders(Object.fromEntries(updates));
      } catch {
        /* keep static fallback */
      }
    };
    refresh();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeOrganization?.slug]);

  const loadProvider = async (kind: string) => {
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/providers/${kind}`, { headers: getHeaders() });
      if (res.ok) {
        const data = await res.json();
        setForm({
          service_kind: kind,
          provider_name: data.provider_name || (providers[kind] || VALID_PROVIDERS[kind])?.[0] || '',
          endpoint_url: data.endpoint_url || '',
          api_key: '',
          model_name: data.model_name || '',
          overrides: data.overrides || {},
        });
        setEditingKind(kind);
      }
    } catch { /* skip */ }
  };

  const saveProvider = async () => {
    setSaving(true);
    setMessage('');
    try {
      const body: any = {
        provider_name: form.provider_name,
        endpoint_url: form.endpoint_url || undefined,
        model_name: form.model_name || undefined,
        overrides: form.overrides || {},
      };
      if (form.api_key) {
        body.api_key = form.api_key;
      }
      const res = await fetch(`${config.apiBaseUrl}/api/providers/${form.service_kind}`, {
        method: 'PUT',
        headers: getHeaders(),
        body: JSON.stringify(body),
      });
      if (res.ok) {
        setMessage('Settings saved.');
        setEditingKind(null);
      } else {
        const err = await res.json().catch(() => ({}));
        setMessage(`Error: ${err.detail || 'Failed to save'}`);
      }
    } catch {
      setMessage('Network error.');
    }
    setSaving(false);
  };

  if (editingKind) {
    return (
      <div className="p-4">
        <button
          onClick={() => setEditingKind(null)}
          className="text-sm text-zinc-400 hover:text-zinc-200 mb-3"
        >
          Back to Providers
        </button>
        <h3 className="text-lg font-medium text-zinc-100 mb-4">
          {SERVICE_LABELS[editingKind]} Settings
        </h3>

        {message && (
          <div className={`mb-3 px-3 py-2 rounded-lg text-sm ${
            message.startsWith('Error') ? 'bg-red-600/20 text-red-400' : 'bg-green-600/20 text-green-400'
          }`}>
            {message}
          </div>
        )}

        <div className="space-y-3 max-w-md">
          <div>
            <label className="block text-sm text-zinc-400 mb-1">Provider</label>
            <select
              value={form.provider_name}
              onChange={e => setForm({ ...form, provider_name: e.target.value })}
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none"
            >
              {(providers[editingKind] || VALID_PROVIDERS[editingKind] || []).map(p => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm text-zinc-400 mb-1">Endpoint URL (optional)</label>
            <input
              type="text"
              value={form.endpoint_url || ''}
              onChange={e => setForm({ ...form, endpoint_url: e.target.value })}
              placeholder="e.g. http://unicorn-litellm:4000/v1"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-500"
            />
          </div>
          <div>
            <label className="block text-sm text-zinc-400 mb-1">Model Name (optional)</label>
            <input
              type="text"
              value={form.model_name || ''}
              onChange={e => setForm({ ...form, model_name: e.target.value })}
              placeholder="e.g. gemma-4-26b-moe"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-500"
            />
          </div>
          <div>
            <label className="block text-sm text-zinc-400 mb-1">API Key (leave blank to keep current)</label>
            <input
              type="password"
              value={form.api_key || ''}
              onChange={e => setForm({ ...form, api_key: e.target.value })}
              placeholder="Enter new API key"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-500"
            />
          </div>
          <button
            onClick={saveProvider}
            disabled={saving}
            className="px-4 py-2 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 disabled:opacity-40 text-white text-sm font-medium transition-colors"
          >
            {saving ? 'Saving...' : 'Save Settings'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="p-4">
      <h3 className="text-lg font-medium text-zinc-100 mb-2">Provider Registry</h3>
      <p className="text-sm text-zinc-400 mb-4">
        Configure which AI services each task uses for this organization.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {Object.entries(SERVICE_LABELS).map(([kind, label]) => (
          <button
            key={kind}
            onClick={() => loadProvider(kind)}
            className="flex items-center justify-between p-3 rounded-lg bg-zinc-900 border border-zinc-800 hover:border-zinc-600 transition-colors text-left"
          >
            <div>
              <span className="text-sm font-medium text-zinc-100">{label}</span>
              <span className="block text-xs text-zinc-500 mt-0.5">
                {(providers[kind] || VALID_PROVIDERS[kind] || [])[0] || 'default'}
              </span>
            </div>
            <span className="text-zinc-600">Configure</span>
          </button>
        ))}
      </div>
    </div>
  );
}
