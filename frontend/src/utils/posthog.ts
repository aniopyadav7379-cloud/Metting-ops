/**
 * PostHog client wrapper. Singleton, lazy-init on first import — we don't
 * want to bundle a network call into the critical path of `npm run build`.
 *
 * Two read paths:
 *   - VITE_POSTHOG_KEY  (required; if absent, init() is a no-op so dev + CI work)
 *   - VITE_POSTHOG_HOST (defaults to https://us.i.posthog.com — PostHog
 *     Cloud US ingestion endpoint, matching the rest of the UC ecosystem)
 *
 * The wrapper exports `track(event, properties?)`, `identify(userId, traits?)`,
 * `reset()` (on logout), and `setOptOut(bool)` for users who disable analytics.
 * Every call is safe to make when init() never ran — they're no-ops.
 *
 * Privacy posture: respects DNT header, respects window.localStorage flag
 * `meetingops.posthog.opt_out` = '1' (set by a future Settings toggle),
 * never records form input automatically, never records session replays
 * by default. Aaron can flip session replay on in the PostHog dashboard
 * once he wants it.
 */
import posthog from 'posthog-js';

/**
 * sessionStorage key the Pricing page writes the chosen {plan, billing_cycle}
 * to before redirecting to Stripe, and that App.tsx reads on the success
 * redirect to fire `subscription_started`. Lives here (not in Pricing) so
 * App can read it without eagerly importing the lazy-loaded Pricing page.
 */
export const PENDING_CHECKOUT_KEY = 'meetingops.posthog.pending_checkout';

let initialized = false;

export function initPostHog(): void {
  if (initialized) return;
  const key = import.meta.env.VITE_POSTHOG_KEY as string | undefined;
  const host =
    (import.meta.env.VITE_POSTHOG_HOST as string | undefined) ?? 'https://us.i.posthog.com';
  if (!key) return; // dev/CI/no-key: silent no-op
  const dnt = navigator.doNotTrack === '1' || (window as any).doNotTrack === '1';
  const optedOut = (() => {
    try {
      return window.localStorage.getItem('meetingops.posthog.opt_out') === '1';
    } catch {
      return false;
    }
  })();
  if (dnt || optedOut) return;
  posthog.init(key, {
    api_host: host,
    capture_pageview: true,
    autocapture: false, // we'll instrument specific events
    disable_session_recording: true, // off by default; flip in dashboard if wanted
    persistence: 'localStorage+cookie',
    person_profiles: 'identified_only', // don't bloat user counts with anonymous
  });
  initialized = true;
}

export function track(event: string, properties?: Record<string, unknown>): void {
  if (!initialized) return;
  try {
    posthog.capture(event, properties);
  } catch {
    /* analytics shouldn't break the app */
  }
}

export function identify(userId: string, traits?: Record<string, unknown>): void {
  if (!initialized) return;
  try {
    posthog.identify(userId, traits);
  } catch {
    /* */
  }
}

export function resetIdentity(): void {
  if (!initialized) return;
  try {
    posthog.reset();
  } catch {
    /* */
  }
}

export function setOptOut(optOut: boolean): void {
  try {
    if (optOut) {
      window.localStorage.setItem('meetingops.posthog.opt_out', '1');
      if (initialized) posthog.opt_out_capturing();
    } else {
      window.localStorage.removeItem('meetingops.posthog.opt_out');
      if (initialized) posthog.opt_in_capturing();
    }
  } catch {
    /* */
  }
}
