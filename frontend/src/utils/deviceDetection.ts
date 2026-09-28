/**
 * Device capability detection.
 *
 * Phase A of the mobile roadmap: skip browser-side STT/LLM loads on
 * phones and tablets. iOS Safari WebGPU is slow/limited and pre-falls
 * back to WASM at <1× real-time, mobile origin-storage caps evict the
 * 1GB+ of cached weights, and background-tab JS throttling kills
 * capture mid-meeting. The user still gets full transcript + summary,
 * just produced by the server reprocess pass at meeting completion
 * instead of live in the browser.
 *
 * This module is the single source of truth for "what can this device
 * actually do." `AlwaysOnContext` reads it once at provider mount and
 * gates Parakeet 0.6B INT8 + Qwen 3 0.6B / Gemma 4 E2B model loads on
 * the result. UI components read it to render the capture-only banner
 * and to hide the live-transcript/summary panes (which would be empty
 * in capture-only mode anyway).
 *
 * iPadOS gotcha: modern iPads send a desktop Mac user-agent. The
 * disambiguator is `navigator.maxTouchPoints > 1` (Macs cap at 0 or 1).
 * We treat iPadOS as mobile because the underlying constraints are the
 * same as iPhone (WebGPU limits, storage caps, battery + thermal).
 */

export type CapabilityClass =
  | 'desktop-capable'    // Modern desktop with WebGPU. Run browser STT + LLM.
  | 'mobile-capable'     // Phone/tablet WITH WebGPU. Run browser STT + LLM (Phase A.6).
  | 'capture-only'       // Mobile/tablet without WebGPU. Audio capture + upload only.
  | 'desktop-fallback';  // Desktop without WebGPU. Try browser inference but expect degraded perf.

export interface DeviceCapability {
  /** True for any phone or tablet (iPhone, Android, iPad). */
  isMobile: boolean;
  /** Specifically iOS Safari / WebKit (iPhone or iPad in iOS UA). */
  isIOS: boolean;
  /** Any Android device. */
  isAndroid: boolean;
  /**
   * iPadOS in modern Mac-UA mode. Detected via touch-points heuristic
   * because Apple removed the iPad UA token in iPadOS 13+.
   */
  isIPadOS: boolean;
  /** Browser exposes the WebGPU API. Does NOT guarantee a working adapter. */
  hasWebGPU: boolean;
  /**
   * SharedArrayBuffer is enabled (requires cross-origin isolation
   * headers). Some onnxruntime-web threading paths need this; without
   * it Parakeet falls back to single-threaded WASM which is much
   * slower.
   */
  hasSharedArrayBuffer: boolean;
  /**
   * `navigator.deviceMemory` if exposed (Chromium only, in GB, capped
   * at 8). Null on Safari/Firefox.
   */
  deviceMemoryGB: number | null;
  /** Bucketed capability used by callers to gate behavior. */
  capabilityClass: CapabilityClass;
}

/**
 * Run all detection probes once and bucket the result.
 *
 * Designed to be called at provider/context mount and cached. The probes
 * are cheap individually but we don't want to re-run them on every
 * render. Safe to call multiple times — no side effects.
 */
export function detectDevice(): DeviceCapability {
  // SSR guard. During build-time pre-render `navigator` is undefined;
  // we report a safe desktop-capable default so the server-rendered
  // HTML matches the hydrated client when WebGPU is present.
  if (typeof navigator === 'undefined' || typeof window === 'undefined') {
    return {
      isMobile: false,
      isIOS: false,
      isAndroid: false,
      isIPadOS: false,
      hasWebGPU: false,
      hasSharedArrayBuffer: false,
      deviceMemoryGB: null,
      capabilityClass: 'desktop-capable',
    };
  }

  const ua = navigator.userAgent || '';
  const platform = navigator.platform || '';
  const maxTouch = typeof navigator.maxTouchPoints === 'number' ? navigator.maxTouchPoints : 0;

  // Direct UA matches. Order matters: iPad (legacy UA) before iPhone
  // before generic Apple to keep the flags mutually exclusive.
  const isIPhone = /iPhone|iPod/.test(ua);
  const isLegacyIPad = /iPad/.test(ua);
  // iPadOS 13+ reports as Macintosh. Disambiguate by touch-points —
  // Macs report 0 or 1, iPads report 5+. We also require platform to
  // be Mac-like to avoid false positives on touchscreen Chromebooks
  // (which set maxTouchPoints high but report a Linux platform).
  const isModernIPad = /Macintosh|MacIntel/.test(platform)
    && maxTouch > 1;
  const isIPadOS = isLegacyIPad || isModernIPad;

  const isIOS = isIPhone || isIPadOS;
  const isAndroid = /Android/.test(ua);

  const isMobile = isIOS || isAndroid;

  // WebGPU availability. `'gpu' in navigator` is the canonical probe.
  // We don't await `requestAdapter()` here because that's async and
  // can prompt for permission in some browsers; the model loader does
  // the real check at load time and falls back to WASM if the adapter
  // fails. For routing purposes "is the API even exposed" is enough.
  const hasWebGPU = typeof (navigator as any).gpu !== 'undefined';

  // SharedArrayBuffer requires the page to be cross-origin-isolated
  // (COOP+COEP headers). We probe both the global and the
  // crossOriginIsolated flag because some Safari versions leak the
  // global without enabling the headers.
  const sharedArrayBufferAvailable = typeof SharedArrayBuffer !== 'undefined'
    && (typeof window !== 'undefined' && (window as any).crossOriginIsolated === true);
  const hasSharedArrayBuffer = sharedArrayBufferAvailable;

  // navigator.deviceMemory is Chromium-only and quantized to powers
  // of two with a max of 8GB (a Chromium fingerprinting mitigation).
  const memoryAny = (navigator as any).deviceMemory;
  const deviceMemoryGB = typeof memoryAny === 'number' && memoryAny > 0 ? memoryAny : null;

  let capabilityClass: CapabilityClass;
  if (isMobile) {
    // Phase A.6: phones/tablets that expose WebGPU run on-device STT +
    // summary just like desktop (live transcript + live summary, IDB
    // buffer). Older phones without WebGPU stay capture-only — WASM-only
    // inference is too slow/hot to auto-enable; the server does the work
    // at Stop for them.
    capabilityClass = hasWebGPU ? 'mobile-capable' : 'capture-only';
  } else if (hasWebGPU) {
    capabilityClass = 'desktop-capable';
  } else {
    // Desktop without WebGPU. Browser inference still runs via WASM
    // but is slow. We warn the user but keep the path on — Firefox
    // doesn't have WebGPU by default and we don't want to break it.
    capabilityClass = 'desktop-fallback';
  }

  return {
    isMobile,
    isIOS,
    isAndroid,
    isIPadOS,
    hasWebGPU,
    hasSharedArrayBuffer,
    deviceMemoryGB,
    capabilityClass,
  };
}

/**
 * Should this device attempt to load Parakeet 0.6B INT8 + the small
 * browser LLM? True on desktop (with or without WebGPU; WASM fallback
 * handles the no-WebGPU desktop case) AND, as of Phase A.6, on
 * WebGPU-capable phones/tablets (`mobile-capable`). False only on
 * `capture-only` devices (phones/tablets without WebGPU).
 */
export function shouldRunBrowserInference(cap: DeviceCapability): boolean {
  return cap.capabilityClass !== 'capture-only';
}

/**
 * Gate for the STOP-TIME full-audio local pass. It is heavier than the
 * live streaming inference `shouldRunBrowserInference` covers: the whole
 * meeting is re-transcribed and the LLM then re-summarizes the full
 * transcript.
 *
 * Since 2026-07-17 the pass runs CHUNKED (`inBrowserSTT.transcribeLong`):
 * ~5-minute silence-aligned windows with a yielding mel loop, so compute
 * per step is bounded and the page stays responsive. What remains
 * unbounded is the decoded 16 kHz mono PCM held for the whole pass
 * (~3.8 MB per minute of audio) plus the browser's transient decode
 * peak — that is what these caps protect:
 *
 * - `desktop-fallback` (no WebGPU → WASM/CPU): never. Even chunked, the
 *   pass can take longer than the meeting itself on CPU.
 * - Chromium reports deviceMemory quantized to ≤ 8 GB: machines under
 *   8 GB cap at ~1 h of audio (decoded ~230 MB); 8 GB-class or unknown
 *   (non-Chromium WebGPU browsers) cap at ~3 h (~690 MB decoded).
 *
 * When the gate says no, the caller keeps the live-quality summary as the
 * final record and says so — a degraded summary beats a dead laptop.
 */
export function shouldRunFullLocalPass(
  cap: DeviceCapability,
  audioBytes: number,
): { ok: boolean; reason?: string } {
  if (cap.capabilityClass === 'capture-only') {
    return { ok: false, reason: 'this device is capture-only' };
  }
  if (cap.capabilityClass === 'desktop-fallback') {
    return {
      ok: false,
      reason: 'this browser has no GPU acceleration (WebGPU), so the full-quality pass would run on CPU and could take longer than the meeting itself',
    };
  }
  const MB = 1024 * 1024;
  // Opus/WebM meeting audio runs ~0.5-1 MB/min; use bytes as the duration proxy.
  const lowMemory = cap.deviceMemoryGB !== null && cap.deviceMemoryGB < 8;
  const capBytes = lowMemory ? 60 * MB : 180 * MB;
  if (audioBytes > capBytes) {
    return {
      ok: false,
      reason: lowMemory
        ? 'the recording is too long to decode in this device’s memory (about an hour is the limit here)'
        : 'the recording is too long for an in-browser pass (about three hours is the limit)',
    };
  }
  return { ok: true };
}

/**
 * Should this device buffer the recording to IndexedDB (durable,
 * survives a tab suspend/kill) rather than volatile memory? True for the
 * devices that run full local inference — `desktop-capable` and, as of
 * Phase A.6, `mobile-capable`. `capture-only` and `desktop-fallback`
 * keep the in-memory buffer (the recording is uploaded whole at Stop; on
 * those a kill mid-recording loses the in-memory buffer).
 */
export function supportsLocalPersistence(cap: DeviceCapability): boolean {
  return cap.capabilityClass === 'desktop-capable'
    || cap.capabilityClass === 'mobile-capable';
}

/**
 * Should the UI show the "capture-only mode" banner at the top of the
 * recording surface? True only for phones and tablets — desktop fallback
 * still runs browser inference (degraded) and shows the normal panes.
 */
export function shouldShowCaptureOnlyBanner(cap: DeviceCapability): boolean {
  return cap.capabilityClass === 'capture-only';
}
