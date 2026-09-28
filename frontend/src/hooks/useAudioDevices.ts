import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AUTO_PREFER_USB_EVENT,
  PREFERRED_DEVICE_EVENT,
  getAutoPreferUsb,
  getPreferredDeviceId,
  isUsbAudioDevice,
  setPreferredDeviceId,
} from '../utils/audioDevicePreference';

/**
 * Inspired by LiveKit's `useMediaDeviceSelect` + the `createMediaDeviceObserver`
 * pattern (web/node_modules/@livekit/components-core/src/observables/room.ts).
 *
 * Differences from LiveKit:
 *   - No track switching — we hand the active deviceId back to the caller
 *     and let them (VAD engine, MediaRecorder, etc.) do the actual mic
 *     swap. Keeps the hook decoupled from any specific recording path.
 *   - Permission-prompt-on-mount is opt-in. Most callers (e.g. Settings)
 *     should leave it OFF and call `primePermissions()` only when the user
 *     explicitly clicks a "show device names" button. Until permissions
 *     are granted, device labels are empty strings; we render
 *     "Microphone 1", "Microphone 2", etc. instead.
 *
 * Returns:
 *   - `devices`: filtered to `audioinput` only
 *   - `activeDeviceId`: current localStorage preference, or null = default
 *   - `setActiveDevice(id|null)`: persist + notify
 *   - `refresh()`: re-enumerate on demand
 *   - `permissionState`: 'unknown' | 'granted' | 'denied' | 'prompt'
 *   - `primePermissions()`: getUserMedia → stop tracks → re-enumerate so
 *     labels are populated
 *   - `error`: last enumeration error, if any
 */

export type AudioDevicePermissionState =
  | 'unknown'
  | 'granted'
  | 'denied'
  | 'prompt';

export interface UseAudioDevicesValue {
  devices: MediaDeviceInfo[];
  activeDeviceId: string | null;
  setActiveDevice: (id: string | null) => void;
  autoPreferUsb: boolean;
  refresh: () => Promise<void>;
  primePermissions: () => Promise<boolean>;
  permissionState: AudioDevicePermissionState;
  error: string | null;
}

export interface UseAudioDevicesOptions {
  /** If true, attempts to prime permissions on mount. Defaults false. */
  requestPermissionsOnMount?: boolean;
  /** Called when a new USB mic is detected via devicechange. */
  onUsbAppeared?: (device: MediaDeviceInfo) => void;
  /** Called when the previously-active device disappears. */
  onActiveDeviceLost?: (lostDeviceId: string) => void;
}

export function deviceFriendlyName(
  device: MediaDeviceInfo,
  index: number,
): string {
  if (device.label && device.label.trim()) return device.label;
  if (device.deviceId === 'default') return 'System default';
  if (device.deviceId === 'communications') return 'Communications default';
  return `Microphone ${index + 1}`;
}

/**
 * Sort device list so the "best for recording" candidates are first:
 *   1. USB mics (more likely the intentional pick)
 *   2. Non-default named devices
 *   3. "default" and "communications" pseudo-devices last
 * Stable within each band.
 */
function sortDevices(devices: MediaDeviceInfo[]): MediaDeviceInfo[] {
  return [...devices].sort((a, b) => {
    const aIsPseudo = a.deviceId === 'default' || a.deviceId === 'communications';
    const bIsPseudo = b.deviceId === 'default' || b.deviceId === 'communications';
    if (aIsPseudo !== bIsPseudo) return aIsPseudo ? 1 : -1;
    const aUsb = isUsbAudioDevice(a);
    const bUsb = isUsbAudioDevice(b);
    if (aUsb !== bUsb) return aUsb ? -1 : 1;
    return 0;
  });
}

export function useAudioDevices(
  options: UseAudioDevicesOptions = {},
): UseAudioDevicesValue {
  const {
    requestPermissionsOnMount = false,
    onUsbAppeared,
    onActiveDeviceLost,
  } = options;

  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [activeDeviceId, setActiveDeviceIdState] = useState<string | null>(() =>
    getPreferredDeviceId(),
  );
  const [autoPreferUsb, setAutoPreferUsbState] = useState<boolean>(() =>
    getAutoPreferUsb(),
  );
  const [permissionState, setPermissionState] =
    useState<AudioDevicePermissionState>('unknown');
  const [error, setError] = useState<string | null>(null);

  // Track previous device list so we can diff for USB appearance / device loss
  const prevDevicesRef = useRef<MediaDeviceInfo[]>([]);
  const onUsbAppearedRef = useRef(onUsbAppeared);
  const onActiveDeviceLostRef = useRef(onActiveDeviceLost);
  useEffect(() => {
    onUsbAppearedRef.current = onUsbAppeared;
  }, [onUsbAppeared]);
  useEffect(() => {
    onActiveDeviceLostRef.current = onActiveDeviceLost;
  }, [onActiveDeviceLost]);

  const refresh = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) {
      setError('Browser does not support device enumeration.');
      return;
    }
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      const inputs = all.filter((d) => d.kind === 'audioinput');
      const sorted = sortDevices(inputs);
      setDevices(sorted);
      setError(null);

      // Diff against previous list to fire USB-appeared / active-lost
      const prev = prevDevicesRef.current;
      if (prev.length > 0) {
        const prevIds = new Set(prev.map((d) => d.deviceId));
        const currentIds = new Set(inputs.map((d) => d.deviceId));

        // Newly-appeared devices
        for (const d of inputs) {
          if (!prevIds.has(d.deviceId) && isUsbAudioDevice(d)) {
            onUsbAppearedRef.current?.(d);
          }
        }

        // Active device disappeared
        const active = getPreferredDeviceId();
        if (active && prevIds.has(active) && !currentIds.has(active)) {
          onActiveDeviceLostRef.current?.(active);
        }
      }
      prevDevicesRef.current = inputs;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const queryPermission = useCallback(async () => {
    // Permissions API for microphone is widely supported in Chromium and
    // increasingly in Firefox/Safari, but we treat the absence as "unknown"
    // rather than erroring.
    const permissions = (navigator as Navigator & {
      permissions?: {
        query: (descriptor: { name: string }) => Promise<PermissionStatus>;
      };
    }).permissions;
    if (!permissions?.query) {
      setPermissionState('unknown');
      return 'unknown' as const;
    }
    try {
      const status = await permissions.query({ name: 'microphone' });
      const state = status.state as AudioDevicePermissionState;
      setPermissionState(state);
      status.onchange = () => {
        setPermissionState(status.state as AudioDevicePermissionState);
        // A grant means we can now see labels — re-enumerate.
        if (status.state === 'granted') {
          void refresh();
        }
      };
      return state;
    } catch {
      setPermissionState('unknown');
      return 'unknown' as const;
    }
  }, [refresh]);

  const primePermissions = useCallback(async (): Promise<boolean> => {
    if (!navigator.mediaDevices?.getUserMedia) return false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: false,
      });
      // Immediately release the priming stream — we only wanted the labels
      // (browsers withhold labels until at least one permission has been
      // granted on this origin).
      stream.getTracks().forEach((t) => t.stop());
      setPermissionState('granted');
      await refresh();
      return true;
    } catch (e) {
      // NotAllowedError = denied. We still want to refresh so the (label-less)
      // list shows up.
      const name = (e as DOMException)?.name;
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        setPermissionState('denied');
      }
      await refresh();
      return false;
    }
  }, [refresh]);

  // Mount: query permission, initial enumeration, devicechange listener
  useEffect(() => {
    let cancelled = false;

    (async () => {
      const state = await queryPermission();
      if (cancelled) return;

      if (requestPermissionsOnMount && state !== 'granted') {
        await primePermissions();
      } else {
        await refresh();
      }
    })();

    const onDeviceChange = () => {
      void refresh();
    };

    if (typeof navigator !== 'undefined' && navigator.mediaDevices) {
      navigator.mediaDevices.addEventListener('devicechange', onDeviceChange);
    }

    return () => {
      cancelled = true;
      if (typeof navigator !== 'undefined' && navigator.mediaDevices) {
        navigator.mediaDevices.removeEventListener(
          'devicechange',
          onDeviceChange,
        );
      }
    };
  }, [queryPermission, primePermissions, refresh, requestPermissionsOnMount]);

  // Keep activeDeviceId in sync with cross-tab changes & explicit dispatches
  useEffect(() => {
    const onPref = (event: Event) => {
      const detail = (event as CustomEvent<string | null>).detail;
      setActiveDeviceIdState(detail ?? null);
    };
    const onAutoPref = (event: Event) => {
      const detail = (event as CustomEvent<boolean>).detail;
      if (typeof detail === 'boolean') setAutoPreferUsbState(detail);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === 'meetingops.audio.preferredDeviceId') {
        setActiveDeviceIdState(getPreferredDeviceId());
      } else if (event.key === 'meetingops.audio.autoPreferUsb') {
        setAutoPreferUsbState(getAutoPreferUsb());
      }
    };
    window.addEventListener(PREFERRED_DEVICE_EVENT, onPref as EventListener);
    window.addEventListener(AUTO_PREFER_USB_EVENT, onAutoPref as EventListener);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(PREFERRED_DEVICE_EVENT, onPref as EventListener);
      window.removeEventListener(
        AUTO_PREFER_USB_EVENT,
        onAutoPref as EventListener,
      );
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const setActiveDevice = useCallback((id: string | null) => {
    setPreferredDeviceId(id);
    setActiveDeviceIdState(id);
  }, []);

  return {
    devices,
    activeDeviceId,
    setActiveDevice,
    autoPreferUsb,
    refresh,
    primePermissions,
    permissionState,
    error,
  };
}
