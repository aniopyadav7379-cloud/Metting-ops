// Persistence + capability gating for the in-browser STT toggle.
// Kept separate from inBrowserSTT.ts so the settings UI can import it
// without dragging in the ORT runtime fetch path.

export const STT_MODE_STORAGE_KEY = 'meetingops.stt.mode.v1';

export type SttMode = 'browser-parakeet' | 'server-parakeet-1.1b';

const VALID_MODES: SttMode[] = ['browser-parakeet', 'server-parakeet-1.1b'];

export interface SttCapability {
  /** True if the browser can plausibly run Parakeet INT8 inference. */
  available: boolean;
  /** Reason we'd fall back to server, if available is false. */
  reason: string | null;
}

/**
 * Conservative capability check. We don't try to instantiate WebGPU here
 * because that has side effects; we just check the static surface. ORT
 * itself will downgrade WebGPU→WASM at session creation time, so
 * "WASM-only" devices still count as available.
 */
export function getSTTCapability(): SttCapability {
  if (typeof window === 'undefined') {
    return { available: false, reason: 'No window object.' };
  }
  if (typeof WebAssembly === 'undefined' || typeof WebAssembly.instantiate !== 'function') {
    return { available: false, reason: 'WebAssembly is not available.' };
  }
  if (typeof AudioContext === 'undefined' && typeof (window as any).webkitAudioContext === 'undefined') {
    return { available: false, reason: 'Web Audio API is not available.' };
  }
  // We hit caches.open() inside the loader; missing CacheStorage is fine
  // (we just re-download on every load), so it doesn't gate availability.
  return { available: true, reason: null };
}

export function getSTTMode(): SttMode {
  if (typeof window === 'undefined') return 'browser-parakeet';
  const cap = getSTTCapability();
  if (!cap.available) return 'server-parakeet-1.1b';
  try {
    const stored = window.localStorage.getItem(STT_MODE_STORAGE_KEY) as SttMode | null;
    if (stored && VALID_MODES.includes(stored)) return stored;
  } catch {
    /* localStorage disabled in private modes — fall through. */
  }
  return 'browser-parakeet';
}

export function setSTTMode(mode: SttMode): void {
  if (typeof window === 'undefined') return;
  if (!VALID_MODES.includes(mode)) return;
  try {
    window.localStorage.setItem(STT_MODE_STORAGE_KEY, mode);
    window.dispatchEvent(
      new CustomEvent<SttMode>('meetingops:stt-mode', { detail: mode }),
    );
  } catch {
    /* noop */
  }
}
