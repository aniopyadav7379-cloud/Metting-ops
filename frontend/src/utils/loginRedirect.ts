let inFlight = false;

function isUnicornCommanderHost(): boolean {
  return (
    typeof window !== "undefined" &&
    /(?:^|\.)unicorncommander\.ai$/i.test(window.location.hostname)
  );
}

function isMagicUnicornHost(): boolean {
  return (
    typeof window !== "undefined" &&
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

  return `/#/login`;
}

export function redirectToLogin(rd?: string): void {
  if (inFlight) return;

  inFlight = true;

  const target =
    rd ?? `${window.location.pathname}${window.location.hash}`;

  window.location.href = ssoStartUrl(target);
}
