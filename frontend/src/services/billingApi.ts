// Billing API client — wraps /api/billing/{checkout,portal,subscription}.
//
// All three endpoints require the JWT token. If they 503, billing is
// not configured on this instance (test sandbox, on-prem appliance
// without Stripe) — callers should fall back to the mailto: CTA the
// Pricing page already has, not show a broken-flow toast.
//
// The Checkout + Portal flows are window.location redirects (we don't
// open them in a popup). Stripe's hosted Checkout handles the rest:
// success_url and cancel_url are configured server-side.

import { config } from '../config';

export interface CheckoutResponse {
  url: string;
}

export interface PortalResponse {
  url: string;
}

export interface SubscriptionState {
  tier: string;
  is_founding_member: boolean;
  founding_cohort?: string | null;
  has_subscription: boolean;
  subscription_id?: string | null;
  status?: string | null;
  price_id?: string | null;
  current_period_end?: string | null;
  cancel_at_period_end: boolean;
  // True when the backend runs Stripe in TEST mode (STRIPE_TEST_MODE=1).
  // The Pricing page renders a "no real charges" banner so a test
  // checkout isn't mistaken for a live one.
  test_mode?: boolean;
}

export class BillingUnavailableError extends Error {
  constructor(message = 'Billing is not configured on this instance.') {
    super(message);
    this.name = 'BillingUnavailableError';
  }
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  try {
    const token = localStorage.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
  } catch {
    /* ignore storage failures */
  }
  return headers;
}

async function postBilling<T>(path: string, body?: object): Promise<T> {
  const r = await fetch(`${config.apiBaseUrl}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 503) {
    throw new BillingUnavailableError();
  }
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const data = await r.json();
      if (data?.detail) detail = data.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await r.json()) as T;
}

export type BillingCycle = 'monthly' | 'annual';

/**
 * v3.23.0: the 5-tier matrix. Free has no checkout (it's the default
 * on signup). Enterprise has no self-serve checkout — it routes to a
 * sales mailto from the Pricing card. Basic / Pro / Suite are the only
 * tiers that actually call `/api/billing/checkout`.
 */
export type CheckoutPlan = 'basic' | 'pro' | 'suite' | 'enterprise';

/**
 * Start a Stripe Checkout session for the given plan and redirect the
 * browser to the hosted Checkout URL.
 *
 * `plan` is one of the v3.23.0 paid tiers ('basic' | 'pro' | 'suite').
 * Passing 'enterprise' is a no-op — Enterprise is a sales conversation,
 * not a self-serve Checkout — and the Pricing page wires that CTA to a
 * mailto directly instead of calling this function.
 *
 * `billingCycle` defaults to 'monthly'. 'annual' maps server-side to
 * the matching annual price ID for the tier (each saves 2 months vs the
 * monthly rate). Backend resolves the actual Stripe price ID; the
 * frontend never references price IDs directly.
 *
 * Returns void — the redirect happens synchronously. Callers wanting
 * to handle errors should `await startCheckout(...)` inside a try/catch
 * and toast on the rethrown error.
 */
export async function startCheckout(
  plan: CheckoutPlan | string,
  billingCycle: BillingCycle = 'monthly',
): Promise<void> {
  if (plan === 'enterprise') {
    // Enterprise has no self-serve checkout. The Pricing page renders
    // a mailto CTA for this tier; if a caller ends up here anyway, do
    // nothing visible — never POST 'enterprise' to /api/billing/checkout.
    return;
  }
  const data = await postBilling<CheckoutResponse>('/api/billing/checkout', {
    plan,
    billing_cycle: billingCycle,
  });
  if (data.url) {
    window.location.href = data.url;
  } else {
    throw new Error('Checkout URL not returned');
  }
}

/**
 * Start a Stripe Billing Portal session and redirect the browser to it.
 * Used for the "View existing subscription" link on /pricing.
 */
export async function startPortal(): Promise<void> {
  const data = await postBilling<PortalResponse>('/api/billing/portal');
  if (data.url) {
    window.location.href = data.url;
  } else {
    throw new Error('Portal URL not returned');
  }
}

/**
 * Read the current user's subscription state. Returns `has_subscription:
 * false` if billing is unconfigured or the user has no Stripe customer.
 * Throws only on real network/auth errors.
 */
export async function fetchSubscription(): Promise<SubscriptionState> {
  const r = await fetch(`${config.apiBaseUrl}/api/billing/subscription`, {
    headers: authHeaders(),
  });
  if (!r.ok) {
    throw new Error(`${r.status} ${r.statusText}`);
  }
  return (await r.json()) as SubscriptionState;
}
