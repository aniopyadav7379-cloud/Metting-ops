import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Mic, Settings as SettingsIcon } from 'lucide-react';
import { useAudioDevices, deviceFriendlyName } from '../hooks/useAudioDevices';
import { isUsbAudioDevice } from '../utils/audioDevicePreference';

/**
 * Per-session microphone indicator + override dropdown. Shows which mic is
 * currently captured, and lets the user pick a different one mid-session
 * WITHOUT changing the persistent default in Settings.
 *
 * The "actual" deviceId comes from the engine (could differ from the
 * stored preference if a fallback fired); the dropdown writes back via
 * the parent's `onSelect` handler, which is wired into the
 * AlwaysOnContext hot-swap path. This component does not touch
 * localStorage — that's intentional, so a per-session override doesn't
 * leak into the saved default.
 */

export interface SessionMicChipProps {
  /** The deviceId we're currently capturing from (may be null = system default). */
  activeDeviceId: string | null;
  /** Called when the user picks a different device for THIS session only. */
  onSelect: (deviceId: string | null) => void;
  /** Render disabled while engine is mid-switch. */
  disabled?: boolean;
}

export default function SessionMicChip({
  activeDeviceId,
  onSelect,
  disabled = false,
}: SessionMicChipProps) {
  const { devices, primePermissions, permissionState } = useAudioDevices();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onDocClick = (event: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  // Prime permissions the first time the user opens the dropdown — until
  // then, we render label-less placeholders. Most sessions have already
  // had permission granted by the time the user gets here (they had to
  // grant to start the VAD engine), but if Settings was never opened and
  // the session started via @start-always-on we may need a kick.
  useEffect(() => {
    if (open && permissionState !== 'granted' && permissionState !== 'denied') {
      void primePermissions();
    }
  }, [open, permissionState, primePermissions]);

  const activeDevice = devices.find((d) => d.deviceId === activeDeviceId) || null;
  const activeLabel = activeDevice
    ? deviceFriendlyName(activeDevice, 0)
    : 'System default';
  const activeIsUsb = activeDevice ? isUsbAudioDevice(activeDevice) : false;

  const handleSelect = (id: string | null) => {
    setOpen(false);
    onSelect(id);
  };

  return (
    <div ref={containerRef} className="relative inline-block">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex max-w-[260px] items-center gap-1.5 rounded-full border border-zinc-700 bg-zinc-800/70 px-3 py-1 text-xs font-medium text-zinc-200 transition hover:border-zinc-600 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-60"
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Click to change input device for this session"
      >
        <Mic className="h-3.5 w-3.5 text-zinc-400" />
        <span className="truncate">
          {activeLabel}
          {activeIsUsb ? ' (USB)' : ''}
        </span>
        <ChevronDown className="h-3.5 w-3.5 text-zinc-500" />
      </button>

      {open && (
        <div
          className="absolute right-0 z-30 mt-1 w-72 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900 shadow-xl"
          role="listbox"
          aria-label="Select microphone for this session"
        >
          <div className="border-b border-zinc-800 px-3 py-2 text-[11px] uppercase tracking-wide text-zinc-500">
            Mic for this session
          </div>

          <button
            type="button"
            onClick={() => handleSelect(null)}
            className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition hover:bg-zinc-800 ${
              activeDeviceId === null ? 'text-emerald-300' : 'text-zinc-200'
            }`}
            role="option"
            aria-selected={activeDeviceId === null}
          >
            <span>System default</span>
            {activeDeviceId === null && <span className="text-xs">●</span>}
          </button>

          <div className="max-h-64 overflow-y-auto">
            {devices.map((device, idx) => {
              const id = device.deviceId;
              const isActive = id === activeDeviceId;
              const usb = isUsbAudioDevice(device);
              return (
                <button
                  key={id || `dev-${idx}`}
                  type="button"
                  onClick={() => handleSelect(id || null)}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition hover:bg-zinc-800 ${
                    isActive ? 'text-emerald-300' : 'text-zinc-200'
                  }`}
                  role="option"
                  aria-selected={isActive}
                >
                  <span className="truncate">
                    {deviceFriendlyName(device, idx)}
                    {usb && (
                      <span className="ml-1 text-[10px] text-amber-300">(USB)</span>
                    )}
                  </span>
                  {isActive && <span className="ml-2 shrink-0 text-xs">●</span>}
                </button>
              );
            })}
            {devices.length === 0 && (
              <div className="px-3 py-3 text-xs text-zinc-500">
                No input devices detected.
              </div>
            )}
          </div>

          <a
            href="/settings/audio"
            className="flex items-center gap-1.5 border-t border-zinc-800 px-3 py-2 text-[11px] text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
          >
            <SettingsIcon className="h-3 w-3" />
            Change default in Settings…
          </a>
        </div>
      )}
    </div>
  );
}
