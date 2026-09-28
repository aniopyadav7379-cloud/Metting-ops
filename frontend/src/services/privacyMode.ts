// Persistence + helpers for the local-only privacy mode toggle.
//
// When ON, no recording bytes, transcripts, slices, or summaries leave
// the browser. Sessions are stored in IndexedDB (see localSessionStore)
// and rolled up at session end via the in-browser LLM.
//
// Force-disabled when WebGPU is unavailable — the LLM rollup needs it
// (Parakeet STT works WASM-only via ORT). Callers should still check
// getWebGPUStatus() before flipping the toggle; this module exposes
// `isPrivacyModeAvailable()` so the UI can match.

export const PRIVACY_MODE_LOCAL_ONLY_KEY = 'meetingops.privacyMode.localOnly';

export function getLocalOnly(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(PRIVACY_MODE_LOCAL_ONLY_KEY) === '1';
  } catch {
    return false;
  }
}

export function setLocalOnly(enabled: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(PRIVACY_MODE_LOCAL_ONLY_KEY, enabled ? '1' : '0');
    window.dispatchEvent(
      new CustomEvent<boolean>('meetingops:privacy-mode', { detail: enabled }),
    );
  } catch {
    /* noop */
  }
}

/**
 * Privacy mode is "available" when WebGPU is plausibly usable — the
 * in-browser LLM final-summary roll-up needs it. Browser STT downgrades
 * to WASM on its own, so we only block on the LLM side here.
 */
export function isPrivacyModeAvailable(): boolean {
  if (typeof navigator === 'undefined') return false;
  const nav = navigator as Navigator & { gpu?: unknown };
  return !!nav.gpu;
}
