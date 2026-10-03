/**
 * Authentication redirect routing.
 *
 * Deployment modes:
 *   - *.unicorncommander.ai
 *       Native backend OIDC / Keycloak flow.
 *
 *   - *.magicunicorn.dev
 *       oauth2-proxy flow.
 *
 *   - Vercel / Render / localhost / other deployments
 *       Consumer JWT login at /#/login.
 *
 * Vercel does NOT run oauth2-proxy, so sending it to /oauth2/start
 * causes the SPA to get stuck on "Redirecting to sign-in...".
 */

let inFlight = false;

function isUnicornCommanderHost(): boolean {
  return (
    typeof window !== 'undefined' &&
    /(?:^|\.)unicorncommander\.ai$/i.test(window.location.hostname)
  );
}

function isMagicUnicornHost(): boolean {
  return (
    typeof window !== 'undefined' &&
    /(?:^|\.)magicunicorn\.dev$/i.test(window.location.hostname)
  );
}

export function ssoStartUrl(target: string): string {
  if (isUnicornCommanderHost()) {
    return `/api/auth/sso/uc/start?returnTo=${encodeURIComponent(target)}`;
  }

  if (isMagicUnicornHost()) {
    return `/oauth2/start?rd=${encodeURIComponent(target)}`;
  }

  // Vercel, Render, localhost and other consumer deployments
  // use the normal JWT email/username + password login page.
  return `/#/login`;
}

export function redirectToLogin(rd?: string): void {
  if (inFlight) return;

  inFlight = true;

  const target =
    rd ?? `${window.location.pathname}${window.location.hash}`;

  window.location.href = ssoStartUrl(target);
}