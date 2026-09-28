/**
 * Single-fire guard for /oauth2/start.
 *
 * The SPA has FIVE+ places that redirect to /oauth2/start on auth failure
 * (fetch interceptor, AuthContext, utils/api wrapper, AppRouterSimplified,
 * Login). On initial page load, several fetches can 401 concurrently;
 * without a guard each one fires its own /oauth2/start which lands a
 * fresh PKCE challenge cookie that immediately overwrites the previous
 * one. When the callback comes back referencing the FIRST state, the
 * CSRF cookie holds the SECOND state's verifier → Keycloak rejects
 * with "Code mismatch" and a 500. Seen 2026-05-18.
 *
 * Module-scope flag is sufficient — once we navigate away the entire
 * module reloads.
 */
let inFlight = false;

/**
 * The two deployments authenticate differently:
 *   - PRODUCTION (`*.unicorncommander.ai`): native OIDC — the backend owns the
 *     Keycloak code flow at `/api/auth/sso/uc/start` (no oauth2-proxy here, so
 *     `/oauth2/start` would just fall through to the SPA and bounce the user
 *     back to the landing page).
 *   - dogfood (`*.magicunicorn.dev`) + everything else: oauth2-proxy at
 *     `/oauth2/start`.
 * Derive it from the hostname so one bundle is correct on both nodes.
 */
export function ssoStartUrl(target: string): string {
  const nativeOidc =
    typeof window !== 'undefined' &&
    /(?:^|\.)unicorncommander\.ai$/i.test(window.location.hostname);
  return nativeOidc
    ? `/api/auth/sso/uc/start?returnTo=${encodeURIComponent(target)}`
    : `/oauth2/start?rd=${encodeURIComponent(target)}`;
}

export function redirectToLogin(rd?: string): void {
  if (inFlight) return;
  inFlight = true;
  const target =
    rd ?? `${window.location.pathname}${window.location.hash}`;
  window.location.href = ssoStartUrl(target);
}
