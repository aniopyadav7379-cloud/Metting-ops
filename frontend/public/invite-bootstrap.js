(() => {
  'use strict';

  const storageKey = 'meetingops.pendingInvitationSecret';
  const status = document.getElementById('invite-status');
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const secret = fragment.get('token') || '';

  // Clear the fragment before any navigation. Fragments are not sent in the
  // initial HTTP request, and the secret must not be copied into an SSO query.
  window.history.replaceState({}, '', '/invite-bootstrap.html');

  if (secret.length < 20 || secret.length > 256) {
    if (status) {
      status.textContent =
        'This invitation link is incomplete. Ask the sender for a fresh link.';
    }
    return;
  }

  try {
    window.sessionStorage.setItem(storageKey, secret);
  } catch {
    if (status) {
      status.textContent =
        'This browser could not secure the invitation. Enable session storage and try again.';
    }
    return;
  }

  const returnTo = '/shared/sessions';
  const nativeOidc = /(?:^|\.)unicorncommander\.ai$/i.test(
    window.location.hostname,
  );
  const loginUrl = nativeOidc
    ? `/api/auth/sso/uc/start?returnTo=${encodeURIComponent(returnTo)}`
    : `/oauth2/start?rd=${encodeURIComponent(returnTo)}`;

  // Same-tab navigation preserves origin-scoped sessionStorage across the
  // Keycloak round trip while the SSO request contains no invitation secret.
  window.location.replace(loginUrl);
})();
