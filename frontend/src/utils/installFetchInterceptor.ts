/**
 * Global fetch interceptor — the single, consolidated window.fetch wrapper.
 *
 * Installs ONCE at module load time (imported from main.tsx). It does BOTH
 * jobs that used to live in two separate, order-fragile monkeypatches:
 *
 *   1. Active-org + auth header injection (was an effect in AuthContext).
 *      Same-origin /api/* requests get the `X-MeetingOps-Org` header (from
 *      the stored active-org slug) and an `Authorization: Bearer` header
 *      (from the stored access token) when not already present. The org
 *      rules come from utils/organization.ts (isMeetingOpsApiRequest +
 *      buildOrgHeaders) so the decoration boundary is identical to before.
 *
 *   2. Stale-session / 401 handling. Detects:
 *        - 302 redirects to oauth2-proxy / Keycloak (stale session cookie)
 *        - opaque redirects (redirect:'manual') / network CORS failures
 *        - HTML responses when JSON was expected (oauth2-proxy 401 page)
 *        - JSON 401s on a JWT-authed API call → token refresh + retry
 *      On the first three: shows the ReconnectingOverlay and redirects
 *      through /oauth2/start (deferred while a recording is live). The JWT
 *      refresh+retry is delegated to a handler AuthContext registers via
 *      setAuthRefreshHandler() (AuthContext owns the token state).
 *
 * Consolidating into one wrapper removes the ordering hazard: previously
 * AuthContext's effect re-wrapped window.fetch on every token change and
 * restored the pre-wrap fetch on cleanup, which could blow away the org
 * header injection depending on mount order.
 *
 * Active-recording guard
 * -----------------------
 * Long-running captures regularly outlive the oauth2-proxy cookie_refresh
 * window (60 min on bigboy). The first audio-chunk save after refresh
 * fails would otherwise force `window.location.href = /oauth2/start`,
 * killing the recording mid-meeting. Instead, when the recording
 * contexts have marked the session as live via `setRecordingActive(true)`
 * we DEFER the redirect: the overlay surfaces with a "session expired —
 * tap to resume" prompt, the failing fetch still returns 401 so the
 * caller can buffer/retry locally, and the redirect only fires once
 * recording actually stops (or the user explicitly opts in).
 */
import { setReconnectingState } from '../components/ReconnectingOverlay';
import { redirectToLogin } from './loginRedirect';
import { config } from '../config';
import {
  buildOrgHeaders,
  getStoredOrgSlug,
  isMeetingOpsApiRequest,
} from './organization';

const _originalFetch = window.fetch.bind(window);

// Module-level claim set the recording contexts flip — see AlwaysOnContext +
// RecordingContext. Each context owns a distinct key (`alwaysOn`,
// `recordingContext`) so neither one's stop-handler ever clears the
// other's claim by mistake.
const activeRecordingClaims = new Set<string>();
let redirectDeferred = false;

export function setRecordingActive(key: string, active: boolean): void {
  const wasActive = activeRecordingClaims.size > 0;
  if (active) activeRecordingClaims.add(key);
  else activeRecordingClaims.delete(key);
  const isActive = activeRecordingClaims.size > 0;
  // Recording just stopped (no claims remaining) AND we had previously
  // deferred a redirect → surface it now so the user re-auths instead of
  // staring at a dead overlay.
  if (wasActive && !isActive && redirectDeferred) {
    redirectDeferred = false;
    setTimeout(() => { redirectToLogin(); }, 50);
  }
}

/**
 * JWT 401 refresh handler. AuthContext owns the access/refresh-token state,
 * so it registers a callback here that performs the refresh and resolves
 * `true` when a fresh access token is in localStorage. The interceptor uses
 * it to refresh-and-retry a JWT-authed API call that came back 401. Null
 * until AuthContext mounts (SSO-only / pre-mount calls just fall through).
 */
type AuthRefreshHandler = () => Promise<boolean>;
let authRefreshHandler: AuthRefreshHandler | null = null;

export function setAuthRefreshHandler(handler: AuthRefreshHandler | null): void {
  authRefreshHandler = handler;
}

function isApiUrl(urlStr: string): boolean {
  // Same-origin /api/* OR absolute https://meetingops.../api/*
  if (urlStr.startsWith('/api/') || urlStr.startsWith('/ws/')) return true;
  try {
    const u = new URL(urlStr, window.location.origin);
    return (u.pathname.startsWith('/api/') || u.pathname.startsWith('/ws/')) &&
           u.origin === window.location.origin;
  } catch {
    return false;
  }
}

function triggerReconnect() {
  try { setReconnectingState(true); } catch { /* component may not be mounted yet */ }
  if (activeRecordingClaims.size > 0) {
    // Hold the redirect — recording is live and we'd lose the capture if
    // we kicked the user to /oauth2/start. The overlay stays up; the
    // setRecordingActive(false) callback runs the redirect once recording
    // actually stops. The failing fetch still returns 401, so any
    // caller-side retry/buffer logic kicks in normally.
    redirectDeferred = true;
    return;
  }
  // Use a small delay so React has a frame to render the overlay
  setTimeout(() => { redirectToLogin(); }, 50);
}

const interceptedFetch: typeof fetch = async (input, init) => {
  const urlStr = typeof input === 'string'
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url;

  if (!isApiUrl(urlStr)) {
    // Pass through non-API calls untouched
    return _originalFetch(input as RequestInfo, init);
  }

  // ── Header injection (active-org + auth) ───────────────────────────────
  // Decorate same-origin /api requests with the active-org header + the
  // stored bearer token. isMeetingOpsApiRequest is the exact boundary the
  // old AuthContext interceptor used; buildOrgHeaders only sets the header
  // when an org slug exists and the caller didn't already set it.
  const orgSlug = getStoredOrgSlug();
  const storedToken =
    (typeof localStorage !== 'undefined' && localStorage.getItem('access_token')) || null;
  let shouldDecorate = false;
  try {
    shouldDecorate = isMeetingOpsApiRequest(urlStr, config.apiBaseUrl);
  } catch {
    shouldDecorate = false;
  }

  const headers = shouldDecorate
    ? buildOrgHeaders(init?.headers, orgSlug)
    : new Headers(init?.headers || {});
  if (shouldDecorate && storedToken && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${storedToken}`);
  }
  // Force JSON Accept header so oauth2-proxy is more likely to return
  // a 401/JSON instead of an HTML login page if it can't find a session.
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');

  // redirect: 'manual' so we can detect the 302 to Keycloak ourselves;
  // letting the browser follow the redirect would CORS-fail at auth.<domain>.
  const finalInit: RequestInit = {
    ...init,
    headers,
    redirect: init?.redirect || 'manual',
  };

  let response: Response;
  try {
    response = await _originalFetch(input as RequestInfo, finalInit);
  } catch (err) {
    // A network error on an API call almost always means the browser
    // tried to follow a cross-origin redirect (oauth2-proxy → Keycloak)
    // and CORS-blocked. Treat as stale session.
    triggerReconnect();
    throw err;
  }

  // Manual-mode opaque redirect → status 0, type === 'opaqueredirect'
  if (response.type === 'opaqueredirect' || response.status === 0 || response.status === 302) {
    triggerReconnect();
    return new Response(JSON.stringify({ detail: 'Reconnecting…' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // HTML body when JSON was expected → oauth2-proxy unauth page or similar
  const ct = response.headers.get('content-type') || '';
  if (ct.startsWith('text/html')) {
    triggerReconnect();
    return new Response(JSON.stringify({ detail: 'Reconnecting…' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // JSON 401 on a JWT-authed API call → try a token refresh + retry once.
  // Skip /auth/* so a failed login/refresh doesn't recurse. The handler is
  // owned by AuthContext (it holds the refresh token); when it isn't
  // registered yet we just return the 401 untouched.
  if (
    response.status === 401 &&
    authRefreshHandler &&
    !/\/auth\//.test(urlStr)
  ) {
    const refreshed = await authRefreshHandler();
    const refreshedToken =
      (typeof localStorage !== 'undefined' && localStorage.getItem('access_token')) || null;
    if (refreshed && refreshedToken && shouldDecorate) {
      const retryHeaders = buildOrgHeaders(init?.headers, orgSlug);
      retryHeaders.set('Authorization', `Bearer ${refreshedToken}`);
      if (!retryHeaders.has('Accept')) retryHeaders.set('Accept', 'application/json');
      const retryInit: RequestInit = {
        ...init,
        headers: retryHeaders,
        redirect: init?.redirect || 'manual',
      };
      const retried = await _originalFetch(input as RequestInfo, retryInit);
      if (retried.status !== 401) return retried;
      response = retried;
    }
  }

  // JSON 401 with no recoverable JWT — the SSO-cookie path. oauth2-proxy
  // answers API routes with a bare 401/{} when its session dies (Keycloak
  // idle timeout beats the 24h cookie), and the checks above never fire:
  // no redirect, no HTML, nothing to refresh. Without this branch the app
  // keeps rendering from cache while every button silently does nothing —
  // exactly what the 2026-07-16 user test reported ("Export Summary does
  // not work", "PDF not working"). Gate on evidence a session existed
  // (stored org slug / token) so anonymous visitors on the public landing
  // are never bounced to login by a legitimately-401 probe.
  if (
    response.status === 401 &&
    !/\/auth\//.test(urlStr) &&
    (Boolean(orgSlug) || Boolean(storedToken))
  ) {
    triggerReconnect();
  }

  return response;
};

// Install once at module load. Idempotent — won't double-wrap on HMR or if
// some other module imports this twice.
if (!(window as unknown as { __meetOpsFetchInterceptor?: boolean }).__meetOpsFetchInterceptor) {
  window.fetch = interceptedFetch;
  (window as unknown as { __meetOpsFetchInterceptor?: boolean }).__meetOpsFetchInterceptor = true;
}

export {};
