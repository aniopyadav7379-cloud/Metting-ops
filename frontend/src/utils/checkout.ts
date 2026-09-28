// Shared "buy Pro" / "manage subscription" helpers.
// -----------------------------------------------------------------------------
// Every in-app upgrade affordance (UpgradeBanner, /pricing, Landing Pro card,
// Settings → Plan & billing) funnels through `beginProCheckout` so the Stripe
// redirect, the analytics stash, and the error/toast handling are written once.
//
// The billing API client (services/billingApi.ts) already owns the network
// contract with the LIVE backend (`POST /api/billing/checkout` + `/portal`).
// These helpers only wire that client to the UI: they never touch Stripe
// directly and never reference a price ID (the server resolves the `pro`
// plan → Stripe price). Frontend-only, no backend contract change.
// -----------------------------------------------------------------------------

import {
  startCheckout,
  startPortal,
  BillingUnavailableError,
} from '../services/billingApi';
import { showToast } from '../components/Toast';
import { track, PENDING_CHECKOUT_KEY } from './posthog';

/** Paid tiers — used to decide "upgrade" vs "manage subscription" UI. */
const PAID_TIERS = new Set(['basic', 'pro', 'suite', 'enterprise']);

/** True when the tier is a paid plan (i.e. the user already has a subscription). */
export function isPaidTier(tier?: string | null): boolean {
  return !!tier && PAID_TIERS.has(tier);
}

/**
 * Best-effort "is this browser signed in" check WITHOUT depending on
 * AuthContext. Used by surfaces that render outside the AuthProvider in tests
 * (the Landing page). A missing token routes anonymous visitors to signup;
 * checkout itself still requires a valid JWT server-side, so this is only a
 * routing hint, never an authorization decision.
 */
export function hasStoredAuthToken(): boolean {
  try {
    return !!window.localStorage.getItem('access_token');
  } catch {
    return false;
  }
}

/**
 * Start the Pro Stripe Checkout for the signed-in user and redirect to the
 * hosted Checkout page. On success this navigates away (no return). On failure
 * it toasts and stays put.
 *
 * `source` is an analytics label for where the click came from
 * (e.g. 'upgrade_banner', 'pricing_page', 'settings_billing', 'landing').
 */
export async function beginProCheckout(source: string): Promise<void> {
  // Stash the chosen plan/cycle so App.tsx can fire `subscription_started`
  // when Stripe redirects back to `#/sessions?upgrade=success`. Matches the
  // contract App.tsx + posthog.ts already expect (PENDING_CHECKOUT_KEY).
  try {
    window.sessionStorage.setItem(
      PENDING_CHECKOUT_KEY,
      JSON.stringify({ plan: 'pro', billing_cycle: 'monthly' }),
    );
  } catch {
    /* sessionStorage disabled — only affects post-purchase analytics attribution */
  }
  track('checkout_started', { plan: 'pro', billing_cycle: 'monthly', source });

  try {
    await startCheckout('pro'); // redirects to Stripe on success
  } catch (err) {
    if (err instanceof BillingUnavailableError) {
      showToast.info(
        'Online checkout isn’t enabled on this instance. Contact support to upgrade.',
      );
    } else {
      showToast.error(
        err instanceof Error
          ? err.message
          : 'Could not start checkout. Please try again.',
      );
    }
  }
}

/**
 * Open the Stripe Billing Portal (manage / cancel / update payment) for an
 * existing subscriber and redirect there. Toasts and stays put on failure.
 */
export async function manageSubscription(): Promise<void> {
  try {
    await startPortal(); // redirects to the Stripe Billing Portal on success
  } catch (err) {
    if (err instanceof BillingUnavailableError) {
      showToast.info('Subscription management isn’t available on this instance.');
    } else {
      showToast.error(
        err instanceof Error
          ? err.message
          : 'Could not open the billing portal. Please try again.',
      );
    }
  }
}
