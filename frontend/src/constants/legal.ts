// COUNSEL-REVIEW
// -----------------------------------------------------------------------------
// In-app legal copy — single source of truth. The strings below are legal
// statements; counsel should edit THIS file, not the component call sites.
//
// The recording-consent copy powers the in-app consent prompt promised by
// AUP §2.3. It is shown once per browser session, before recording starts, on
// every recording surface (desktop always-on + mobile capture).
// -----------------------------------------------------------------------------

/** Title of the pre-record consent modal. COUNSEL-REVIEW. */
export const RECORDING_CONSENT_TITLE = 'Before you record';

/**
 * Body copy of the pre-record consent modal. COUNSEL-REVIEW.
 * Names the all-party-consent US states + the EU explicitly.
 */
export const RECORDING_CONSENT_BODY =
  'This session will be recorded and transcribed. You are responsible for ' +
  'obtaining consent from all participants where required by law — including ' +
  'all-party-consent states (CA, FL, IL, MD, MA, MT, NH, PA, WA) and the EU.';

/** Required-checkbox label on the consent modal. COUNSEL-REVIEW. */
export const RECORDING_CONSENT_CHECKBOX_LABEL =
  'I confirm I have the necessary consent from all participants.';

/**
 * sessionStorage key for the per-session consent flag. Client-side only for
 * v1 (follow-up: persist server-side against the session/recording record).
 * `sessionStorage` scopes to one browser session — cleared when the tab
 * closes, so the prompt re-appears in the next session.
 */
const RECORDING_CONSENT_STORAGE_KEY = 'mo_recording_consent_v1';

/** True when the user already confirmed recording consent this session. */
export function hasRecordingConsent(): boolean {
  try {
    return sessionStorage.getItem(RECORDING_CONSENT_STORAGE_KEY) === 'true';
  } catch {
    // Private mode / storage disabled: fail closed so we re-prompt each start.
    return false;
  }
}

/** Remember (for this browser session) that recording consent was confirmed. */
export function rememberRecordingConsent(): void {
  try {
    sessionStorage.setItem(RECORDING_CONSENT_STORAGE_KEY, 'true');
  } catch {
    /* storage disabled: no-op — the gate simply re-prompts on the next start */
  }
}
