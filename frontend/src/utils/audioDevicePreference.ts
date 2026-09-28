/**
 * Single source of truth for the user's preferred audio input device.
 *
 * Two persisted settings:
 *   - `meetingops.audio.preferredDeviceId` — browser deviceId string, or
 *     empty/null to mean "use system default".
 *   - `meetingops.audio.autoPreferUsb` — boolean (default true). When ON,
 *     plugging in a USB mic surfaces a toast prompting the user to switch.
 *
 * Why these are *browser* deviceIds and not the PipeWire `hw:0,0` IDs the
 * legacy AudioSettings UI tracked: the VAD engine and all MediaRecorder
 * paths call `navigator.mediaDevices.getUserMedia()`, which only accepts
 * browser-namespace deviceIds. The backend `/api/simple/audio-devices`
 * endpoint enumerates server-side PipeWire devices (used by the appliance
 * build's native recording path) and lives in a totally separate ID space.
 *
 * The localStorage keys are NOT cross-device-portable (browser deviceIds
 * are origin-pinned, per the spec). Don't sync them via cloud settings.
 */

export const PREFERRED_DEVICE_ID_KEY = 'meetingops.audio.preferredDeviceId';
export const AUTO_PREFER_USB_KEY = 'meetingops.audio.autoPreferUsb';

export const PREFERRED_DEVICE_EVENT = 'meetingops:audio-preferred-device';
export const AUTO_PREFER_USB_EVENT = 'meetingops:audio-auto-prefer-usb';

export interface AudioDevicePreferences {
  preferredDeviceId: string | null;
  autoPreferUsb: boolean;
}

function safeLocalStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null;
    return window.localStorage;
  } catch {
    return null;
  }
}

export function getPreferredDeviceId(): string | null {
  const ls = safeLocalStorage();
  if (!ls) return null;
  const raw = ls.getItem(PREFERRED_DEVICE_ID_KEY);
  if (!raw || raw === 'null' || raw === 'undefined') return null;
  return raw;
}

export function setPreferredDeviceId(id: string | null): void {
  const ls = safeLocalStorage();
  if (!ls) return;
  if (id && id.trim()) {
    ls.setItem(PREFERRED_DEVICE_ID_KEY, id);
  } else {
    ls.removeItem(PREFERRED_DEVICE_ID_KEY);
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent<string | null>(PREFERRED_DEVICE_EVENT, { detail: id }),
    );
  }
}

export function getAutoPreferUsb(): boolean {
  const ls = safeLocalStorage();
  if (!ls) return true;
  const raw = ls.getItem(AUTO_PREFER_USB_KEY);
  // Default ON (matches existing AudioSettings behavior where isDefault
  // was implicitly USB-or-system-default).
  if (raw === null) return true;
  return raw === 'true';
}

export function setAutoPreferUsb(value: boolean): void {
  const ls = safeLocalStorage();
  if (!ls) return;
  ls.setItem(AUTO_PREFER_USB_KEY, value ? 'true' : 'false');
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent<boolean>(AUTO_PREFER_USB_EVENT, { detail: value }),
    );
  }
}

export function getAudioPreferences(): AudioDevicePreferences {
  return {
    preferredDeviceId: getPreferredDeviceId(),
    autoPreferUsb: getAutoPreferUsb(),
  };
}

export interface AudioConstraintOptions {
  /** Overrides the localStorage-stored deviceId. Use `null` for system default. */
  deviceId?: string | null;
  /**
   * When true (default), use `{ exact: id }` so getUserMedia fails fast if
   * the device is gone. When false, use `{ ideal: id }` so the browser
   * falls back to the next-best device silently.
   */
  exactDevice?: boolean;
  /** Disable echo cancellation (defaults to true). */
  echoCancellation?: boolean;
  /** Disable noise suppression (defaults to true). */
  noiseSuppression?: boolean;
  /** Disable auto gain control (defaults to true). */
  autoGainControl?: boolean;
}

/**
 * Build the `audio` constraint for a `getUserMedia({ audio, video: false })`
 * call. Centralized so every recording path (VAD, MobileLiveRecording,
 * DesktopBrowserRecorder) honors the same user preference.
 *
 * If the requested device is not available, callers should be prepared to
 * fall back: catch `OverconstrainedError` (when `exactDevice: true`) and
 * retry with `{ deviceId: undefined }` to use the system default.
 */
export function buildAudioConstraints(
  options: AudioConstraintOptions = {},
): MediaTrackConstraints {
  const deviceId =
    options.deviceId !== undefined ? options.deviceId : getPreferredDeviceId();
  const exact = options.exactDevice ?? true;

  const constraints: MediaTrackConstraints = {
    echoCancellation: options.echoCancellation ?? true,
    noiseSuppression: options.noiseSuppression ?? true,
    autoGainControl: options.autoGainControl ?? true,
  };

  if (deviceId) {
    constraints.deviceId = exact ? { exact: deviceId } : { ideal: deviceId };
  }

  return constraints;
}

/**
 * Wrap `getUserMedia({ audio, video: false })` with built-in fallback when
 * an exact device constraint fails. Returns the resulting `MediaStream` AND
 * the actual deviceId we ended up using (which may differ from the
 * requested one if a fallback fired).
 *
 * `requestedDeviceId === null` => no preference, system default.
 */
export async function openMicrophoneStream(
  requestedDeviceId: string | null,
  extra: Omit<AudioConstraintOptions, 'deviceId' | 'exactDevice'> = {},
): Promise<{ stream: MediaStream; deviceId: string | null; fellBack: boolean }> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('Microphone access is not supported in this browser.');
  }

  // First try: exact device id (if specified)
  if (requestedDeviceId) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: buildAudioConstraints({
          deviceId: requestedDeviceId,
          exactDevice: true,
          ...extra,
        }),
        video: false,
      });
      return { stream, deviceId: requestedDeviceId, fellBack: false };
    } catch (err) {
      const name = (err as DOMException)?.name;
      if (name !== 'OverconstrainedError' && name !== 'NotFoundError') {
        // Not a device-missing problem (e.g. NotAllowedError). Re-throw.
        throw err;
      }
      // Fall through to the system-default attempt below.
    }
  }

  // Second try: no deviceId constraint, system default
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: buildAudioConstraints({
      deviceId: null,
      ...extra,
    }),
    video: false,
  });
  return {
    stream,
    deviceId: null,
    fellBack: requestedDeviceId !== null,
  };
}

/**
 * Heuristic: does this device label look like a USB microphone? Used by
 * the "auto-prefer USB" toggle and for the (USB) badge in the dropdown.
 *
 * Conservative: we'd rather miss a USB mic than mis-label a built-in.
 * Tokens picked from observed Chrome/Firefox labels on Linux + macOS.
 */
export function isUsbAudioDevice(device: MediaDeviceInfo): boolean {
  const label = (device.label || '').toLowerCase();
  if (!label) return false;
  // Common USB mic patterns. The literal "usb" token is the strongest
  // signal but plenty of devices (Yeti, MV7, AT2020USB+, Samson, Rode) name
  // themselves without it.
  if (label.includes('usb')) return true;
  if (label.includes('yeti')) return true;
  if (label.includes('mv7')) return true;
  if (label.includes('mv88')) return true;
  if (label.includes('shure')) return true;
  if (label.includes('rode ') || label.endsWith('rode')) return true;
  if (label.includes('samson')) return true;
  if (label.includes('at2020') || label.includes('at2035')) return true;
  if (label.includes('blue ')) return true;
  if (label.includes('focusrite')) return true;
  if (label.includes('scarlett')) return true;
  return false;
}
