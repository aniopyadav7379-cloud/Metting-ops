/**
 * Per-org integrations API client (v3.19.0).
 *
 * Wraps /api/integrations/me/* — the surface that backs the
 * Workspace → Integrations settings panel.
 *
 * Secrets contract: GET endpoints never return the cleartext API key.
 * They return `has_api_key: boolean` + `api_key_last4: string` for
 * masked display. PUT accepts cleartext (encrypted server-side via
 * Fernet) and never echoes it back in the response. Tests:
 * backend/tests/test_integrations_api.py.
 */

import { config } from '../config';

export type IntegrationKey =
  | 'brigade'
  | 'project_ops'
  | 'contact_ops'
  | 'accounting_ops'
  | 'stable'
  | 'jira'
  | 'todoist'
  | 'clickup'
  | 'notion'
  | 'confluence'
  | 'slack'
  | 'teams'
  | 'email';

export interface IntegrationView {
  key: IntegrationKey;
  enabled: boolean;
  api_base_url: string;
  has_api_key: boolean;
  /** Last 4 chars of the cleartext (post-decrypt) for masked display. */
  api_key_last4: string;
  /** Integration-specific safe fields (no secrets). */
  extra: Record<string, unknown>;
}

export interface IntegrationUpdatePayload {
  enabled?: boolean;
  api_base_url?: string;
  /**
   * api_key semantics:
   *   - undefined / not set -> backend leaves the existing ciphertext alone
   *   - ''                  -> backend clears the stored credential
   *   - any string          -> backend Fernet-encrypts and stores
   */
  api_key?: string;
  // Brigade-only
  tenancy_mode?: 'shared' | 'per_org_graph' | 'per_org_instance';
  shared_agent_id?: string;
  // Project-Ops-only
  default_project_number?: string;
  /**
   * When true, action items from completed meetings are auto-submitted to
   * the Project-Ops triage inbox (propose-only). Default false (opt-in).
   */
  auto_push_action_items?: boolean;
  // Email-only
  smtp_host?: string;
  smtp_port?: number;
  smtp_username?: string;
  smtp_from_address?: string;
  smtp_use_tls?: boolean;
}

export interface TestResult {
  ok: boolean;
  error?: string | null;
}

function baseUrl(): string {
  return `${config.apiBaseUrl}/api/integrations/me`;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: string;
    try {
      const body = await res.json();
      detail = body?.detail || body?.error || `HTTP ${res.status}`;
    } catch {
      detail = `HTTP ${res.status}`;
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export async function listIntegrations(): Promise<IntegrationView[]> {
  const res = await fetch(baseUrl(), { credentials: 'include' });
  return jsonOrThrow<IntegrationView[]>(res);
}

export async function getIntegration(key: IntegrationKey): Promise<IntegrationView> {
  const res = await fetch(`${baseUrl()}/${key}`, { credentials: 'include' });
  return jsonOrThrow<IntegrationView>(res);
}

export async function updateIntegration(
  key: IntegrationKey,
  payload: IntegrationUpdatePayload,
): Promise<IntegrationView> {
  const res = await fetch(`${baseUrl()}/${key}`, {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<IntegrationView>(res);
}

export async function disableIntegration(
  key: IntegrationKey,
): Promise<IntegrationView> {
  const res = await fetch(`${baseUrl()}/${key}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  return jsonOrThrow<IntegrationView>(res);
}

export async function testIntegration(key: IntegrationKey): Promise<TestResult> {
  const res = await fetch(`${baseUrl()}/${key}/test`, {
    method: 'POST',
    credentials: 'include',
  });
  return jsonOrThrow<TestResult>(res);
}
