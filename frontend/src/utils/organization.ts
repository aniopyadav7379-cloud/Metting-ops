export interface OrganizationMembership {
  id: number;
  name: string;
  slug: string;
  role: string;
  is_active: boolean;
  // billing-1: the workspace's plan (free/pro/enterprise). Lets the UI show a
  // workspace-upgrade prompt naming the org when it's the limiting factor.
  plan?: string;
}

export const ORG_STORAGE_KEY = 'meetingops.activeOrg';
export const DEFAULT_ORG_SLUG = 'magic-unicorn';
export const ORG_HEADER_NAME = 'X-MeetingOps-Org';

export function getStoredOrgSlug(): string | null {
  try {
    return localStorage.getItem(ORG_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredOrgSlug(slug: string): void {
  try {
    localStorage.setItem(ORG_STORAGE_KEY, slug);
  } catch {
    // Ignore storage failures and rely on in-memory state.
  }
}

export function clearStoredOrgSlug(): void {
  try {
    localStorage.removeItem(ORG_STORAGE_KEY);
  } catch {
    // Ignore storage failures.
  }
}

export function isMeetingOpsApiRequest(url: RequestInfo | URL, apiBaseUrl: string): boolean {
  const rawUrl = typeof url === 'string' ? url : url.toString();
  const resolved = new URL(rawUrl, window.location.origin);
  const apiBase = new URL(apiBaseUrl, window.location.origin);

  if (!resolved.pathname.startsWith('/api/')) {
    return false;
  }

  return resolved.origin === window.location.origin || resolved.origin === apiBase.origin;
}

export function buildOrgHeaders(headers?: HeadersInit, orgSlug?: string | null): Headers {
  const merged = new Headers(headers ?? {});
  if (orgSlug && !merged.has(ORG_HEADER_NAME)) {
    merged.set(ORG_HEADER_NAME, orgSlug);
  }
  return merged;
}

export function appendOrgQuery(url: string, orgSlug?: string | null): string {
  if (!orgSlug) {
    return url;
  }

  const resolved = new URL(url, window.location.origin);
  resolved.searchParams.set('org', orgSlug);

  if (url.startsWith('http://') || url.startsWith('https://')) {
    return resolved.toString();
  }

  return `${resolved.pathname}${resolved.search}${resolved.hash}`;
}

/**
 * Persist the user's "home"/default workspace server-side
 * (PUT /api/organizations/default). Best-effort + non-blocking: callers
 * (OrgContext.switchOrganization) should not await or surface failures —
 * the localStorage sticky still works offline / if this 4xx/5xxes. The
 * consolidated fetch interceptor attaches the Authorization + org header,
 * so this stays a plain same-origin call.
 */
export function setDefaultOrganization(organizationId: number): Promise<void> {
  return fetch('/api/organizations/default', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ organization_id: organizationId }),
  })
    .then(() => undefined)
    .catch(() => {
      // Swallow — the localStorage sticky is the offline source of truth and
      // the next /api/auth/me will reconcile the server default.
    });
}
