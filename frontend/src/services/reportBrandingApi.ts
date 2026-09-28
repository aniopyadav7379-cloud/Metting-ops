import { config } from '../config';

export type ReportBrandMode = 'meeting_ops' | 'workspace' | 'unbranded';

export interface ReportBranding {
  display_name: string;
  accent_color: string;
  default_mode: ReportBrandMode;
  has_logo: boolean;
  logo_data_uri?: string | null;
}

export interface ReportBrandingUpdate {
  display_name: string;
  accent_color: string;
  default_mode: ReportBrandMode;
  logo_data_uri?: string;
  clear_logo?: boolean;
}

function headers(orgSlug?: string | null): Record<string, string> {
  const value: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  const token = localStorage.getItem('access_token');
  if (token) value.Authorization = `Bearer ${token}`;
  if (orgSlug) value['X-MeetingOps-Org'] = orgSlug;
  return value;
}

async function jsonOrThrow<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body?.detail || body?.error || detail;
    } catch {
      // Keep the status-only fallback.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export async function getReportBranding(
  orgSlug?: string | null,
): Promise<ReportBranding> {
  const response = await fetch(`${config.apiBaseUrl}/api/export/branding`, {
    credentials: 'include',
    headers: headers(orgSlug),
  });
  return jsonOrThrow<ReportBranding>(response);
}

export async function updateReportBranding(
  payload: ReportBrandingUpdate,
  orgSlug?: string | null,
): Promise<ReportBranding> {
  const response = await fetch(`${config.apiBaseUrl}/api/export/branding`, {
    method: 'PUT',
    credentials: 'include',
    headers: headers(orgSlug),
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<ReportBranding>(response);
}
