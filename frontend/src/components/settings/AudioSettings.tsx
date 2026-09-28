import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, Lock, Mic, RefreshCw } from 'lucide-react';
import { config } from '../../config';
import { useSettingsContext } from './SettingsContext';
import type { SectionProps } from './SettingsContext';
import { useAudioDevices, deviceFriendlyName } from '../../hooks/useAudioDevices';
import {
  isUsbAudioDevice,
  openMicrophoneStream,
  setAutoPreferUsb,
} from '../../utils/audioDevicePreference';
import AudioMeter from '../AudioMeter';
import MicTestButton from './MicTestButton';

interface AudioDevice {
  id: string;
  name: string;
  sampleRates: number[];
  channels: number;
  isDefault: boolean;
  isUSB: boolean;
}

export default function AudioSettings(_props: SectionProps) {
  const { settings, setSettings, micVolume, setMicVolume, hostCaps } =
    useSettingsContext();
  const [audioDevices, setAudioDevices] = useState<AudioDevice[]>([]);
  const [audioDeviceError, setAudioDeviceError] = useState<string | null>(null);

  // Browser-side device picker (the one the VAD engine + MediaRecorder
  // paths actually use). Separate from the backend PipeWire list — these
  // are two different ID spaces.
  const {
    devices: browserDevices,
    activeDeviceId,
    setActiveDevice,
    autoPreferUsb,
    permissionState,
    primePermissions,
    refresh: refreshBrowserDevices,
    error: browserDevicesError,
  } = useAudioDevices();

  const labelsHidden = useMemo(
    () => browserDevices.some((d) => !d.label),
    [browserDevices],
  );

  // Hold a short-lived test stream for the VU meter, so the user can see
  // levels while they're picking a device. We open this on demand only.
  const [meterStream, setMeterStream] = useState<MediaStream | null>(null);
  const [meterError, setMeterError] = useState<string | null>(null);
  const [meterOpening, setMeterOpening] = useState<boolean>(false);

  useEffect(
    () => () => {
      meterStream?.getTracks().forEach((t) => t.stop());
    },
    [meterStream],
  );

  const openMeter = async () => {
    setMeterError(null);
    setMeterOpening(true);
    try {
      // Use the active device (or system default). Best-effort: if it
      // fails, surface the error inline.
      const opened = await openMicrophoneStream(activeDeviceId);
      meterStream?.getTracks().forEach((t) => t.stop());
      setMeterStream(opened.stream);
    } catch (e) {
      setMeterError(e instanceof Error ? e.message : String(e));
    } finally {
      setMeterOpening(false);
    }
  };

  const closeMeter = () => {
    meterStream?.getTracks().forEach((t) => t.stop());
    setMeterStream(null);
  };

  // Reopen the meter after a device-id change, so the user immediately
  // hears (sees) the level for the new selection.
  useEffect(() => {
    if (meterStream) {
      // Only refresh if the meter was already showing. Closing + reopening
      // intentionally rebuilds the analyser graph in AudioMeter.
      closeMeter();
      void openMeter();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDeviceId]);

  const fetchAudioDevices = async () => {
    try {
      const response = await fetch(`${config.apiUrl}/api/simple/audio-devices`);
      if (response.ok) {
        const data = await response.json();
        const devices = data.devices || data;

        const mappedDevices: AudioDevice[] = devices.map((device: any) => ({
          id: device.deviceId || device.id || `hw:${device.index},0`,
          name: device.name || device.label || 'Unknown Device',
          sampleRates:
            device.sampleRates ||
            device.sample_rates ||
            [device.sample_rate] || [44100, 48000],
          channels: device.channels || 1,
          isDefault:
            device.is_default ||
            device.isDefault ||
            device.name?.includes('USB') ||
            false,
          isUSB:
            device.name?.includes('USB') ||
            device.label?.includes('USB') ||
            false,
        }));

        setAudioDevices(mappedDevices);
        setAudioDeviceError(null);

        const defaultDevice =
          mappedDevices.find((d) => d.isDefault) ||
          mappedDevices.find((d) => d.isUSB) ||
          mappedDevices[0];
        if (defaultDevice && !settings.defaultDevice) {
          setSettings((prev: any) => ({
            ...prev,
            defaultDevice: defaultDevice.id,
            sampleRate: '44100',
          }));
        }
      }
    } catch (error) {
      console.error('Failed to fetch audio devices:', error);
      setAudioDevices([]);
      setAudioDeviceError(
        'Could not detect audio devices. Check that PipeWire is running and the backend is reachable.'
      );
    }
  };

  const adjustMicVolume = async (volume: number) => {
    try {
      const response = await fetch(`${config.apiUrl}/api/simple/audio/volume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device: settings.defaultDevice,
          volume: volume,
        }),
      });
      if (response.ok) {
        setMicVolume(volume);
      }
    } catch (error) {
      console.log('Backend not available for volume control');
      setMicVolume(volume);
    }
  };

  useEffect(() => {
    if (hostCaps.local_audio) {
      fetchAudioDevices();
    }
  }, [hostCaps.local_audio]);

  // Browser device picker is always shown (it's the path the
  // MediaRecorder + VAD engine actually use, regardless of host caps).

  return (
    <div className="space-y-6">
      {/* Browser device picker — primary, applies to all browser recording paths */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Mic className="h-4 w-4 text-zinc-400" />
            <h3 className="text-sm font-semibold text-zinc-100">
              Browser microphone
            </h3>
          </div>
          <button
            type="button"
            onClick={() => refreshBrowserDevices()}
            className="inline-flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-300 transition hover:bg-zinc-700"
            title="Re-enumerate devices"
          >
            <RefreshCw className="h-3 w-3" />
            Refresh
          </button>
        </div>
        <p className="mt-1 text-xs text-zinc-500">
          Used by the always-on VAD engine and in-browser recorders. Selecting a
          device here persists across sessions on this device.
        </p>

        {labelsHidden && permissionState !== 'denied' && (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2">
            <Lock className="mt-0.5 h-4 w-4 text-amber-300" />
            <div className="flex-1 text-xs text-amber-100">
              <p>
                Device names are hidden until you grant microphone access. Click
                below so we can show real names.
              </p>
              <button
                type="button"
                onClick={() => primePermissions()}
                className="mt-2 inline-flex items-center gap-1 rounded-md bg-amber-500/20 px-2 py-1 text-amber-100 transition hover:bg-amber-500/30"
              >
                Show device names
              </button>
            </div>
          </div>
        )}

        {permissionState === 'denied' && (
          <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
            Microphone access is blocked at the browser level. Update the site
            permissions and refresh to pick a device.
          </div>
        )}

        {browserDevicesError && (
          <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
            {browserDevicesError}
          </div>
        )}

        <div className="mt-4">
          <label
            htmlFor="browser-device"
            className="mb-2 block text-xs font-medium text-zinc-300"
          >
            Preferred input device
          </label>
          <select
            id="browser-device"
            value={activeDeviceId ?? ''}
            onChange={(e) => setActiveDevice(e.target.value || null)}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-zinc-200"
          >
            <option value="">System default (let the browser choose)</option>
            {browserDevices.map((device, idx) => (
              <option key={device.deviceId || `dev-${idx}`} value={device.deviceId}>
                {deviceFriendlyName(device, idx)}
                {isUsbAudioDevice(device) ? ' (USB)' : ''}
              </option>
            ))}
          </select>
        </div>

        <label className="mt-3 flex items-center gap-2">
          <input
            type="checkbox"
            checked={autoPreferUsb}
            onChange={(e) => setAutoPreferUsb(e.target.checked)}
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800"
          />
          <span className="text-xs text-zinc-300">
            Auto-suggest switching when a USB microphone is plugged in
          </span>
        </label>

        {/* Live VU meter for the selected device */}
        <div className="mt-4 rounded-lg border border-zinc-800 bg-black/40 p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="text-xs font-medium text-zinc-300">Live level</div>
            {meterStream ? (
              <button
                type="button"
                onClick={closeMeter}
                className="rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-[11px] text-zinc-200 transition hover:bg-zinc-700"
              >
                Stop
              </button>
            ) : (
              <button
                type="button"
                disabled={meterOpening}
                onClick={openMeter}
                className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-[11px] text-emerald-200 transition hover:bg-emerald-500/20 disabled:opacity-60"
              >
                {meterOpening ? 'Opening…' : 'Start'}
              </button>
            )}
          </div>
          <div className="mt-3">
            <AudioMeter
              stream={meterStream}
              compact
              showNumeric
              ariaLabel="Microphone live level"
            />
          </div>
          {meterError && (
            <p className="mt-2 text-[11px] text-red-300">{meterError}</p>
          )}
          <p className="mt-2 text-[11px] text-zinc-500">
            Tap "Start" to live-monitor the selected device. Stop closes the
            stream — meeting recording uses a separate capture.
          </p>
        </div>

        {/* Mic test (record + playback) */}
        <div className="mt-3">
          <MicTestButton deviceId={activeDeviceId} />
        </div>
      </div>

      {/* Backend (PipeWire) device list — only relevant on the appliance
          build, where local Parakeet 1.1B consumes the hw:X,Y device directly. */}
      {hostCaps.local_audio && (
        <>
          {audioDeviceError && audioDevices.length === 0 && (
            <div className="rounded-lg border border-red-800 bg-red-900/20 p-4">
              <div className="flex items-start gap-3">
                <AlertCircle className="mt-0.5 h-5 w-5 text-red-400" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-red-200">
                    Appliance Audio Device Detection Failed
                  </p>
                  <p className="mt-1 text-xs text-red-300">{audioDeviceError}</p>
                  <button
                    onClick={() => {
                      setAudioDeviceError(null);
                      fetchAudioDevices();
                    }}
                    className="mt-2 rounded bg-red-800 px-3 py-1 text-xs text-red-100 transition-colors hover:bg-red-700"
                  >
                    Retry Detection
                  </button>
                </div>
              </div>
            </div>
          )}

          <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <h3 className="text-sm font-semibold text-zinc-100">
              Appliance recording device (PipeWire)
            </h3>
            <p className="mt-1 text-xs text-zinc-500">
              Used by the on-appliance transcription pipeline (Parakeet). Browser
              recording ignores this — pick the browser mic above.
            </p>

            <div className="mt-3">
              <label className="mb-2 block text-sm font-medium text-zinc-300">
                Recording Device
              </label>
              <select
                value={settings.defaultDevice}
                onChange={(e) => {
                  const device = audioDevices.find((d) => d.id === e.target.value);
                  setSettings({
                    ...settings,
                    defaultDevice: e.target.value,
                    sampleRate: device?.sampleRates.includes(44100)
                      ? '44100'
                      : '48000',
                  });
                }}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-zinc-200"
              >
                {audioDevices.map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.name} {device.isUSB && '(USB)'} {device.isDefault && '⭐'}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-zinc-500">
                {audioDevices.find((d) => d.id === settings.defaultDevice)?.isUSB
                  ? '✅ USB microphone detected - optimal for recording'
                  : 'Built-in microphone selected'}
              </p>
            </div>

            {settings.defaultDevice === 'hw:0,0' && (
              <div className="mt-4">
                <label className="mb-2 block text-sm font-medium text-zinc-300">
                  USB Microphone Volume
                </label>
                <div className="flex items-center gap-4">
                  <input
                    type="range"
                    min="0"
                    max="100"
                    value={micVolume}
                    onChange={(e) => adjustMicVolume(parseInt(e.target.value))}
                    className="h-2 flex-1 cursor-pointer appearance-none rounded-lg bg-zinc-700"
                  />
                  <span className="w-12 text-right text-sm text-zinc-200">
                    {micVolume}%
                  </span>
                </div>
                <p className="mt-1 text-xs text-zinc-500">
                  Adjust microphone input sensitivity
                </p>
              </div>
            )}

            <div className="mt-4">
              <label className="mb-2 block text-sm font-medium text-zinc-300">
                Sample Rate
              </label>
              <select
                value={settings.sampleRate}
                onChange={(e) =>
                  setSettings({ ...settings, sampleRate: e.target.value })
                }
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-zinc-200"
              >
                {audioDevices
                  .find((d) => d.id === settings.defaultDevice)
                  ?.sampleRates.map((rate) => (
                    <option key={rate} value={rate}>
                      {rate / 1000} kHz {rate === 44100 && '(USB Mic Native)'}
                    </option>
                  )) || [
                  <option key="44100" value="44100">
                    44.1 kHz
                  </option>,
                  <option key="48000" value="48000">
                    48 kHz
                  </option>,
                ]}
              </select>
              <p className="mt-1 text-xs text-zinc-500">
                Recording at native rate avoids resampling and preserves quality
              </p>
            </div>
          </div>
        </>
      )}

      <div>
        <label className="mb-2 block text-sm font-medium text-zinc-300">
          Transcription Engine
        </label>
        <div className="space-y-1.5 rounded-lg border border-zinc-700 bg-zinc-800/60 px-4 py-3 text-sm text-zinc-300">
          <div className="flex items-start gap-2">
            <span className="mt-1.5 inline-block h-2 w-2 flex-shrink-0 rounded-full bg-emerald-400" />
            <span>
              <strong className="text-zinc-100">Live (in browser):</strong> Parakeet,
              on-device via WebGPU — $0 compute, nothing leaves the device.
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="mt-1.5 inline-block h-2 w-2 flex-shrink-0 rounded-full bg-fuchsia-400" />
            <span>
              <strong className="text-zinc-100">Final pass (server):</strong> Parakeet 1.1B
              transcription + pyannote 3.1 diarization (speaker separation), with a
              Qwen 3.6 35B-A3B-Vision summary.
            </span>
          </div>
        </div>
        <p className="mt-1 text-xs text-zinc-500">
          The transcription model isn’t user-selectable — Meeting-Ops uses Parakeet
          for all transcription (in-browser live + a server completion pass).
          Privacy mode skips the server pass entirely.
        </p>
      </div>

      <div className="space-y-3">
        <label className="flex items-center gap-3">
          <input
            type="checkbox"
            checked={settings.liveTranscription}
            onChange={(e) =>
              setSettings({ ...settings, liveTranscription: e.target.checked })
            }
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800"
          />
          <span className="text-sm text-zinc-300">
            Enable live transcription during recording
          </span>
        </label>

        {/* Voice Activity Detection toggle removed v3.6.1: it persisted a
            preference no recording path ever read (the always-on VAD engine
            ignores it), and the full-session recording is continuous
            regardless of VAD. A real no-VAD live mode would be a separate
            time-based-chunker feature. See reference_meeting_ops_vad_reality. */}
      </div>
    </div>
  );
}
