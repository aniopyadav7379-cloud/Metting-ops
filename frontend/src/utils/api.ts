/**
 * API utility with stale-session recovery.
 *
 * Wraps fetch to detect oauth2-proxy stale-session patterns:
 * - 302 redirect to a login/Keycloak URL
 * - HTML response when JSON was expected
 *
 * On detection: shows "Reconnecting..." overlay and redirects
 * through oauth2-proxy to re-establish the session.
 */
import { setReconnectingState } from '../components/ReconnectingOverlay';
import { config } from '../config';
import { redirectToLogin } from './loginRedirect';

const OAUTH2_PROXY_LOGIN_PATTERN = /\/oauth2\/(start|sign_out)/i;
const KEYCLOAK_PATTERN = /\/realms\/uchub/i;

function isApiPath(url: string): boolean {
  return url.startsWith('/api/') || url.includes('/api/');
}

function getTargetApiBase(): string {
  return config.apiBaseUrl;
}

async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const urlStr = typeof input === 'string' ? input : input.toString();

  // Determine if this is an API request to our backend
  const apiBaseUrl = getTargetApiBase();
  const isApiCall = urlStr.startsWith('/api/') ||
    urlStr.includes('/api/') ||
    urlStr.startsWith(apiBaseUrl + '/api/') ||
    urlStr.includes(apiBaseUrl + '/api/');

  // Set Accept: application/json for API calls that don't already have it
  const headers = new Headers(init?.headers || {});
  if (isApiCall && !headers.has('Accept')) {
    headers.set('Accept', 'application/json');
  }

  // For API calls, use redirect: manual so we can detect 302s ourselves
  // instead of letting the browser follow them (which causes CORS issues
  // when oauth2-proxy redirects to Keycloak on a different domain).
  const fetchInit: RequestInit = {
    ...init,
    headers,
    redirect: isApiCall ? 'manual' : (init?.redirect || 'follow'),
  };

  const response = await fetch(input, fetchInit);

  // If not an API call, return the response as-is
  if (!isApiCall) {
    return response;
  }

  // Detect stale oauth2-proxy session:
  // 1. 302 redirect (manual mode returns status 302 with no body)
  // 2. HTML content-type when we expected JSON
  if (response.status === 302) {
    setReconnectingState(true);
    const redirectTarget = response.headers.get('location') || '';
    const isAuthRedirect =
      OAUTH2_PROXY_LOGIN_PATTERN.test(redirectTarget) ||
      KEYCLOAK_PATTERN.test(redirectTarget);

    if (isAuthRedirect) {
      redirectToLogin();
    }
    // Return a dummy response so callers don't crash
    return new Response(null, { status: response.status });
  }

  // Check for HTML content-type when JSON was expected
  if (response.headers.get('content-type')?.startsWith('text/html')) {
    setReconnectingState(true);
    redirectToLogin();
    return new Response(null, { status: 401 });
  }

  return response;
}

// ── "My Voice" personal voiceprint API ──────────────────────────────
// User-scoped (not org-scoped) endpoints: the voiceprint follows the
// account across workspaces. Auth headers are injected by the global
// fetch interceptor (installFetchInterceptor.ts).

export interface MyVoiceStatus {
  enrolled: boolean;
  sample_count: number;
  embedding_model: string | null;
  updated_at: string | null;
  consent_at: string | null;
}

/** GET /api/me/voice — current user's voiceprint enrollment status. */
export async function getMyVoice(): Promise<MyVoiceStatus> {
  const res = await apiFetch(`${getTargetApiBase()}/api/me/voice`);
  if (!res.ok) {
    throw new Error(`Failed to load voice status (HTTP ${res.status})`);
  }
  return res.json();
}

/** DELETE /api/me/voice — delete the voiceprint (idempotent 204). */
export async function deleteMyVoice(): Promise<void> {
  const res = await apiFetch(`${getTargetApiBase()}/api/me/voice`, {
    method: 'DELETE',
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(`Failed to delete voiceprint (HTTP ${res.status})`);
  }
}

/**
 * Check if a response indicates a stale session that needs recovery.
 * Call this after apiFetch for non-critical endpoints.
 */
export function isStaleSessionResponse(response: Response): boolean {
  return (
    response.status === 302 ||
    response.headers.get('content-type')?.startsWith('text/html') === true
  );
}

export { apiFetch };
