/**
 * Audio source stream opener — covers mic, tab, and mic+tab capture for
 * passive listeners (always-on VAD) AND the explicit Record button.
 *
 * Why this is separate from `audioDevicePreference.ts`:
 *   - audioDevicePreference is about *which microphone* the user picked
 *     and falls back to system default cleanly. It's a thin layer over
 *     getUserMedia and used by the saved-device picker.
 *   - audioSourceStream is about *what we listen to* — mic, tab audio
 *     (via getDisplayMedia), or both mixed via Web Audio. It pulls
 *     openMicrophoneStream() for the mic side so the saved-device
 *     preference still applies.
 *
 * Persisted setting:
 *   - `meetingops.alwayson.audioSource` — one of 'mic' | 'tab' | 'mic+tab'.
 *     Default 'mic' (existing behavior). Independent of the Record button's
 *     own per-component picker (DesktopBrowserRecorder + MobileLiveRecording
 *     each keep their own key — they predate this and we don't want a
 *     surprise shared-state change when one flips the other).
 *
 * getDisplayMedia notes (the messy part):
 *   - Chrome: `{ video: false, audio: true }` is rejected in some versions
 *     (it requires *something* visual to share). We try audio-only first;
 *     if the browser blows up we retry with `{ video: true, audio: true }`
 *     and immediately stop the returned video track so only audio survives.
 *     This is the same dance DesktopBrowserRecorder does today.
 *   - Firefox: getDisplayMedia accepts audio-only on recent versions but
 *     historically required video. Same fallback.
 *   - Safari: getDisplayMedia is supported in 13+ but DOES NOT allow
 *     audio capture from another tab as of 17. We surface that with a
 *     readable error.
 *   - The user MUST tick "Share tab audio" / "Also share system audio"
 *     in the picker, or we get zero audio tracks. We detect and error.
 *
 * Mic+Tab mixing:
 *   - Web Audio AudioContext + MediaStreamAudioSourceNode for each
 *     incoming stream → MediaStreamAudioDestinationNode. Returns the
 *     destination's `.stream`. Both source nodes connect directly to
 *     the destination (1:1 mix; we don't pan or duck).
 *   - The caller is responsible for keeping `cleanup()` alive on the
 *     returned object. When called it stops both raw streams AND closes
 *     the AudioContext, otherwise Chrome leaks contexts on hot reload.
 */

import { openMicrophoneStream } from './audioDevicePreference';

export type AudioSourceMode = 'mic' | 'tab' | 'mic+tab';

export const AUDIO_SOURCE_MODE_KEY = 'meetingops.alwayson.audioSource';
export const AUDIO_SOURCE_MODE_EVENT = 'meetingops:alwayson-audio-source';

function safeLocalStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null;
    return window.localStorage;
  } catch {
    return null;
  }
}

function isAudioSourceMode(value: unknown): value is AudioSourceMode {
  return value === 'mic' || value === 'tab' || value === 'mic+tab';
}

/**
 * Smart device-class default. Aaron's instinct (v3.22.3 2026-05-30):
 * desktop browsers usually mean "I'm on a Zoom/Meet/Teams call and want
 * to capture the meeting audio plus my mic" — so the right default on
 * desktops with getDisplayMedia is `mic+tab`. Phones/tablets can't
 * capture tab audio anyway (Safari blocks getDisplayMedia audio capture
 * on iOS as of 17; Android Chromium is hit-or-miss), and on mobile most
 * meetings are in-person — so `mic` is the right default there.
 *
 * isTabCaptureSupported() is our proxy for "desktop with capability":
 * Chromium-desktop returns true; mobile + Safari + Firefox-without-
 * permission return false. Once the user explicitly picks a mode, their
 * choice is stored and this default is moot.
 */
function defaultAudioSourceMode(): AudioSourceMode {
  return isTabCaptureSupported() ? 'mic+tab' : 'mic';
}

export function getAudioSourceMode(): AudioSourceMode {
  const ls = safeLocalStorage();
  if (!ls) return defaultAudioSourceMode();
  const raw = ls.getItem(AUDIO_SOURCE_MODE_KEY);
  return isAudioSourceMode(raw) ? raw : defaultAudioSourceMode();
}

export function setAudioSourceMode(mode: AudioSourceMode): void {
  const ls = safeLocalStorage();
  if (!ls) return;
  ls.setItem(AUDIO_SOURCE_MODE_KEY, mode);
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent<AudioSourceMode>(AUDIO_SOURCE_MODE_EVENT, { detail: mode }),
    );
  }
}

export function isTabCaptureSupported(): boolean {
  return (
    typeof navigator !== 'undefined'
    && !!navigator.mediaDevices
    && typeof (navigator.mediaDevices as any).getDisplayMedia === 'function'
  );
}

export interface OpenAudioSourceOptions {
  /** Saved-device override for the mic side (only used when mode includes mic). */
  preferredDeviceId?: string | null;
}

export interface OpenedAudioSource {
  /** The stream the VAD/MediaRecorder should consume. */
  stream: MediaStream;
  /** The mode that was actually used (matches request — included for clarity). */
  mode: AudioSourceMode;
  /**
   * Stops every underlying stream + closes the AudioContext (if mixed).
   * Idempotent — safe to call repeatedly.
   */
  cleanup: () => void;
  /**
   * Subscribe to "underlying source dropped" events — most importantly,
   * the tab the user shared was closed. Called at most once per opener.
   * The listener should stop the always-on session and surface a toast.
   */
  onUnexpectedEnd: (listener: (reason: string) => void) => () => void;
  /** The raw mic stream, when mode includes mic. Useful for VU meter wiring. */
  micStream: MediaStream | null;
  /** The raw tab stream, when mode includes tab. */
  tabStream: MediaStream | null;
}

async function openTabAudioOnly(): Promise<MediaStream> {
  if (!isTabCaptureSupported()) {
    throw new Error('Tab audio capture is not supported in this browser.');
  }
  const md = navigator.mediaDevices as unknown as {
    getDisplayMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  };

  // Try audio-only first (newer Chrome accepts this).
  let display: MediaStream | null = null;
  try {
    display = await md.getDisplayMedia({ video: false, audio: true });
  } catch (err) {
    const name = (err as DOMException)?.name;
    // TypeError / InvalidStateError / NotSupportedError => browser requires
    // video. NotAllowedError + AbortError = real user denial, re-throw.
    if (
      name === 'NotAllowedError'
      || name === 'AbortError'
      || name === 'NotFoundError'
    ) {
      throw err;
    }
    // Retry with video, then drop the video track immediately.
    display = await md.getDisplayMedia({ video: true, audio: true });
  }

  const audioTracks = display.getAudioTracks();
  if (audioTracks.length === 0) {
    display.getTracks().forEach((t) => t.stop());
    throw new Error(
      'No tab audio was captured. In the picker dialog, tick "Share tab audio" or "Also share system audio" before clicking Share.',
    );
  }

  // Stop the video track immediately if one was requested for compat.
  display.getVideoTracks().forEach((t) => t.stop());

  // Build a stream that ONLY has the audio tracks — this matches the
  // contract the rest of the always-on pipeline expects.
  return new MediaStream(audioTracks);
}

/**
 * Open an audio source stream for the given mode. Caller MUST call
 * `cleanup()` on the returned object when the session ends.
 */
export async function openAudioSourceStream(
  mode: AudioSourceMode,
  opts: OpenAudioSourceOptions = {},
): Promise<OpenedAudioSource> {
  const wantsMic = mode === 'mic' || mode === 'mic+tab';
  const wantsTab = mode === 'tab' || mode === 'mic+tab';

  let micStream: MediaStream | null = null;
  let tabStream: MediaStream | null = null;
  let audioCtx: AudioContext | null = null;
  let cleanedUp = false;

  const cleanup = () => {
    if (cleanedUp) return;
    cleanedUp = true;
    [micStream, tabStream].forEach((s) => {
      if (!s) return;
      try {
        s.getTracks().forEach((t) => t.stop());
      } catch {
        // ignore
      }
    });
    if (audioCtx) {
      audioCtx.close().catch(() => undefined);
      audioCtx = null;
    }
  };

  try {
    if (wantsMic) {
      // The mic-side openMicrophoneStream handles the saved-device
      // preference + fallback semantics. We do NOT want to silently
      // fall back to default if the user explicitly asked for tab too;
      // the function only falls back on OverconstrainedError, which
      // means "your saved mic is gone" — that's the same behavior the
      // existing mic-only path has.
      const opened = await openMicrophoneStream(opts.preferredDeviceId ?? null, {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      });
      micStream = opened.stream;
    }

    if (wantsTab) {
      tabStream = await openTabAudioOnly();
    }
  } catch (err) {
    cleanup();
    throw err;
  }

  let outStream: MediaStream;
  if (mode === 'mic+tab') {
    if (!micStream || !tabStream) {
      // Defensive — both should be set by now.
      cleanup();
      throw new Error('Mic + Tab mode failed: one of the sources was missing.');
    }
    audioCtx = new AudioContext();
    const dest = audioCtx.createMediaStreamDestination();
    audioCtx.createMediaStreamSource(micStream).connect(dest);
    audioCtx.createMediaStreamSource(tabStream).connect(dest);
    outStream = dest.stream;
  } else if (mode === 'tab') {
    if (!tabStream) {
      cleanup();
      throw new Error('Tab mode failed: no tab audio stream.');
    }
    outStream = tabStream;
  } else {
    if (!micStream) {
      cleanup();
      throw new Error('Mic mode failed: no microphone stream.');
    }
    outStream = micStream;
  }

  // Track-ended listener wiring. For tab mode, this is the critical
  // signal — when the user closes the shared tab the audio track ends.
  // We register listeners on the underlying streams' tracks (NOT on the
  // mixed-output stream's tracks; the AudioContext keeps producing
  // silence even after the source dies, so its tracks never end).
  const endListeners = new Set<(reason: string) => void>();
  let endFired = false;
  const fireEnd = (reason: string) => {
    if (endFired) return;
    endFired = true;
    endListeners.forEach((l) => {
      try {
        l(reason);
      } catch {
        // ignore
      }
    });
  };

  const wireTrackEnded = (stream: MediaStream | null, label: string) => {
    if (!stream) return;
    stream.getAudioTracks().forEach((track) => {
      track.addEventListener('ended', () => fireEnd(label));
    });
  };
  wireTrackEnded(tabStream, 'tab');
  // Mic-side dying is handled by the existing devicechange path in
  // AlwaysOnContext — don't double-fire here for mic+tab mode.

  return {
    stream: outStream,
    mode,
    cleanup,
    micStream,
    tabStream,
    onUnexpectedEnd: (listener) => {
      endListeners.add(listener);
      // If end already fired before subscribe, fire synchronously so we
      // don't deadlock the caller.
      if (endFired) {
        try {
          listener('tab');
        } catch {
          // ignore
        }
      }
      return () => {
        endListeners.delete(listener);
      };
    },
  };
}

/**
 * Human-readable label for UI chips.
 */
export function audioSourceLabel(mode: AudioSourceMode): string {
  if (mode === 'tab') return 'Tab audio';
  if (mode === 'mic+tab') return 'Mic + Tab';
  return 'Microphone';
}
