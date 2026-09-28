import { useCallback, useEffect, useRef, useState } from 'react';
import { Mic, Square, AlertCircle } from 'lucide-react';
import { useUploads } from '../contexts/UploadsContext';
import {
  buildRecordingFilename,
  pickRecorderTarget,
  tapAudioLevel,
  unlockAudioContext,
  type MediaRecorderTarget,
} from '../utils/mobileRecording';
import {
  getPreferredDeviceId,
  openMicrophoneStream,
} from '../utils/audioDevicePreference';
import {
  isAlwaysOnActive,
  setRecordActive,
} from '../utils/recordingPresence';
import ConfirmModal from './ConfirmModal';

type State = 'idle' | 'preparing' | 'recording' | 'finalizing' | 'uploading';
type AudioSource = 'mic' | 'tab' | 'mic+tab';

const TITLE_KEY = 'meetingops.desktop.title.v1';
const SOURCE_KEY = 'meetingops.desktop.audioSource.v1';

function supportsTabCapture(): boolean {
  return typeof navigator !== 'undefined'
    && !!navigator.mediaDevices
    && typeof (navigator.mediaDevices as any).getDisplayMedia === 'function';
}

function formatDuration(seconds: number): string {
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

export default function DesktopBrowserRecorder() {
  const { startUploads } = useUploads();
  const [state, setState] = useState<State>('idle');
  const [title, setTitle] = useState<string>(() => {
    const stored = localStorage.getItem(TITLE_KEY);
    if (stored) return stored;
    const now = new Date();
    return `Meeting ${now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`;
  });
  const [audioSource, setAudioSource] = useState<AudioSource>(() => {
    const v = localStorage.getItem(SOURCE_KEY);
    return (v === 'tab' || v === 'mic+tab') ? v : 'mic';
  });
  const [duration, setDuration] = useState(0);
  const [audioLevel, setAudioLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const [recorderTarget, setRecorderTarget] = useState<MediaRecorderTarget | null>(null);
  // Collision-guard state for the "always-on is already running" prompt.
  // When the user clicks Record while always-on is active we show a
  // confirm modal instead of silently starting two captures.
  const [collisionOpen, setCollisionOpen] = useState(false);
  const tabCaptureAvailable = supportsTabCapture();

  const streamRef = useRef<MediaStream | null>(null);
  const auxStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startTimeRef = useRef<number | null>(null);
  const tickRef = useRef<number | null>(null);
  const levelTapRef = useRef<{ stop: () => void } | null>(null);

  useEffect(() => { setRecorderTarget(pickRecorderTarget()); }, []);
  useEffect(() => { localStorage.setItem(TITLE_KEY, title); }, [title]);
  useEffect(() => { localStorage.setItem(SOURCE_KEY, audioSource); }, [audioSource]);

  useEffect(() => {
    if (state !== 'recording') return;
    const id = window.setInterval(() => {
      if (startTimeRef.current) {
        setDuration(Math.floor((Date.now() - startTimeRef.current) / 1000));
      }
    }, 500);
    tickRef.current = id;
    return () => { window.clearInterval(id); tickRef.current = null; };
  }, [state]);

  const cleanupCapture = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      try { recorderRef.current.stop(); } catch { /* noop */ }
    }
    recorderRef.current = null;
    [streamRef.current, auxStreamRef.current].forEach((s) => {
      if (s) s.getTracks().forEach((t) => t.stop());
    });
    streamRef.current = null;
    auxStreamRef.current = null;
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => undefined);
      audioCtxRef.current = null;
    }
    if (levelTapRef.current) {
      levelTapRef.current.stop();
      levelTapRef.current = null;
    }
    setAudioLevel(0);
    // Belt + suspenders — cleanupCapture is the common drain for both
    // normal stop and error/unmount paths. setRecordActive(false) is
    // idempotent so this can't double-toggle.
    setRecordActive(false);
  }, []);

  useEffect(() => () => cleanupCapture(), [cleanupCapture]);

  const beginRecording = useCallback(async () => {
    setError(null);
    setPermissionDenied(false);

    // Cross-surface collision guard: refuse to start if always-on is
    // already capturing audio. The modal walks the user through stopping
    // the other surface first. Dismissing the modal closes it without
    // starting; the user retries after stopping always-on.
    if (isAlwaysOnActive()) {
      setCollisionOpen(true);
      return;
    }

    setState('preparing');
    unlockAudioContext();

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('This browser cannot capture audio. Update to the latest Chrome, Edge, Firefox, or Safari.');
      setState('idle');
      return;
    }

    let micStream: MediaStream | null = null;
    let tabStream: MediaStream | null = null;
    const wantsMic = audioSource === 'mic' || audioSource === 'mic+tab';
    const wantsTab = audioSource === 'tab' || audioSource === 'mic+tab';

    try {
      if (wantsMic) {
        // Honor the user's preferred mic from Settings. openMicrophoneStream
        // falls back to system default if the saved device is unavailable.
        const opened = await openMicrophoneStream(getPreferredDeviceId(), {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        });
        micStream = opened.stream;
      }
      if (wantsTab) {
        if (!tabCaptureAvailable) {
          throw new Error('Tab audio capture requires a Chromium-based browser');
        }
        const display = await (navigator.mediaDevices as any).getDisplayMedia({
          video: true,
          audio: true,
        });
        const audioTracks = display.getAudioTracks();
        if (audioTracks.length === 0) {
          display.getTracks().forEach((t: MediaStreamTrack) => t.stop());
          throw new Error('No tab audio captured. Tick "Share tab audio" in the picker dialog.');
        }
        display.getVideoTracks().forEach((t: MediaStreamTrack) => t.stop());
        tabStream = new MediaStream(audioTracks);
      }
    } catch (err: any) {
      const denied = err?.name === 'NotAllowedError' || err?.name === 'SecurityError';
      setPermissionDenied(denied);
      micStream?.getTracks().forEach((t) => t.stop());
      tabStream?.getTracks().forEach((t) => t.stop());
      setError(denied
        ? 'Audio access was blocked. Allow microphone/tab capture in your browser and retry.'
        : `Could not start audio capture: ${err?.message ?? err}`);
      setState('idle');
      return;
    }

    let stream: MediaStream;
    if (micStream && tabStream) {
      const ctx = new AudioContext();
      const dest = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(micStream).connect(dest);
      ctx.createMediaStreamSource(tabStream).connect(dest);
      stream = dest.stream;
      audioCtxRef.current = ctx;
      auxStreamRef.current = tabStream;
      streamRef.current = micStream;
    } else if (tabStream) {
      stream = tabStream;
      streamRef.current = tabStream;
    } else if (micStream) {
      stream = micStream;
      streamRef.current = micStream;
    } else {
      setError('No audio source selected.');
      setState('idle');
      return;
    }

    const target = recorderTarget ?? pickRecorderTarget();
    if (!target) {
      setError('MediaRecorder is unavailable in this browser.');
      cleanupCapture();
      setState('idle');
      return;
    }

    let recorder: MediaRecorder;
    try {
      recorder = target.mimeType ? new MediaRecorder(stream, { mimeType: target.mimeType }) : new MediaRecorder(stream);
    } catch (err: any) {
      setError(`Recorder failed to initialize: ${err?.message ?? err}`);
      cleanupCapture();
      setState('idle');
      return;
    }

    chunksRef.current = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onerror = (event: any) => {
      setError(`Recording error: ${event?.error?.message ?? 'unknown'}`);
      cleanupCapture();
      setState('idle');
    };

    recorderRef.current = recorder;
    levelTapRef.current = tapAudioLevel(stream, setAudioLevel);

    recorder.start(2000);
    startTimeRef.current = Date.now();
    setDuration(0);
    setState('recording');
    // Flip the cross-surface presence flag so always-on refuses to start
    // until we stop. Cleared in stopRecording + on unmount.
    setRecordActive(true);
  }, [audioSource, recorderTarget, tabCaptureAvailable, cleanupCapture]);

  const stopRecording = useCallback(async () => {
    if (!recorderRef.current) return;
    setState('finalizing');
    const recorder = recorderRef.current;

    await new Promise<void>((resolve) => {
      recorder.onstop = () => resolve();
      if (recorder.state !== 'inactive') {
        try { recorder.stop(); } catch { resolve(); }
      } else {
        resolve();
      }
    });

    // Recorder is no longer capturing — let always-on start if the user
    // wants to switch surfaces. We clear here (rather than after upload)
    // because the audio capture *itself* is what blocks always-on, not
    // the upload bookkeeping.
    setRecordActive(false);

    const target = recorderTarget ?? pickRecorderTarget();
    const extension = target?.extension ?? 'webm';
    const mimeType = target?.mimeType ?? 'audio/webm';
    const blob = new Blob(chunksRef.current, { type: mimeType });
    cleanupCapture();

    if (blob.size === 0) {
      setError('No audio was captured. Check your microphone and try again.');
      setState('idle');
      return;
    }

    setState('uploading');
    const filename = buildRecordingFilename(extension, title);
    const file = new File([blob], filename, { type: mimeType });

    try {
      await startUploads([file], { action: 'transcribe' });
      setState('idle');
    } catch (err: any) {
      setError(`Upload failed: ${err?.message ?? err}`);
      setState('idle');
    }
  }, [recorderTarget, title, startUploads, cleanupCapture]);

  const handlePrimaryTap = state === 'recording' ? stopRecording : beginRecording;
  const isBusy = state === 'preparing' || state === 'finalizing' || state === 'uploading';
  const buttonLabel =
    state === 'recording' ? 'Stop' :
    state === 'preparing' ? 'Preparing…' :
    state === 'finalizing' ? 'Finalizing…' :
    state === 'uploading' ? 'Uploading…' : 'Record from this browser';

  return (
    <div className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="flex items-center justify-between mb-4 gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Mic className="w-5 h-5 text-fuchsia-400 shrink-0" />
          <h2 className="text-base font-semibold text-white truncate">Browser Recording</h2>
        </div>
        <span className="text-xs text-zinc-500 tabular-nums">{formatDuration(duration)}</span>
      </div>

      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        disabled={state === 'recording' || isBusy}
        placeholder="Meeting title"
        className="w-full mb-3 rounded-lg bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-fuchsia-500 disabled:opacity-60"
      />

      <div className="mb-3">
        <div className="text-xs uppercase tracking-wider text-zinc-400 mb-1.5">Audio source</div>
        <div className="grid grid-cols-3 gap-2">
          {(['mic', 'tab', 'mic+tab'] as AudioSource[]).map((opt) => {
            const disabled = (opt !== 'mic' && !tabCaptureAvailable) || state === 'recording' || isBusy;
            const active = audioSource === opt;
            const label = opt === 'mic' ? 'Mic' : opt === 'tab' ? 'Tab' : 'Mic + Tab';
            return (
              <button
                key={opt}
                type="button"
                disabled={disabled}
                onClick={() => setAudioSource(opt)}
                className={`rounded-lg border px-2 py-2 text-xs transition-colors ${
                  active
                    ? 'bg-fuchsia-600 border-fuchsia-500 text-white'
                    : 'bg-zinc-900 border-zinc-800 text-zinc-200 hover:bg-zinc-800'
                } ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="mb-3">
        <div className="h-2 rounded-full bg-zinc-950 overflow-hidden border border-zinc-800">
          <div
            className="h-full rounded-full bg-gradient-to-r from-fuchsia-500 to-indigo-500 transition-[width] duration-100"
            style={{ width: `${Math.min(100, Math.round(audioLevel * 100))}%` }}
          />
        </div>
      </div>

      <button
        type="button"
        onClick={handlePrimaryTap}
        disabled={isBusy || !recorderTarget}
        className={`w-full min-h-[52px] rounded-xl text-white font-semibold text-sm flex items-center justify-center gap-2 transition-all shadow-lg ${
          state === 'recording'
            ? 'bg-red-600 hover:bg-red-500 shadow-red-900/30'
            : 'bg-gradient-to-r from-fuchsia-600 to-indigo-600 hover:from-fuchsia-500 hover:to-indigo-500 shadow-fuchsia-900/30'
        } ${(isBusy || !recorderTarget) ? 'opacity-60 cursor-not-allowed' : ''}`}
      >
        {state === 'recording' ? <Square className="w-5 h-5" fill="currentColor" /> : <Mic className="w-5 h-5" />}
        <span>{buttonLabel}</span>
      </button>

      {error && (
        <div className={`mt-3 rounded-lg px-3 py-2 text-xs flex items-start gap-2 border ${
          permissionDenied
            ? 'bg-red-900/30 border-red-800/60 text-red-200'
            : 'bg-amber-900/20 border-amber-800/60 text-amber-200'
        }`}>
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <p className="mt-3 text-[11px] text-zinc-500">
        Upload progress appears in the tray at the bottom right of the screen.
      </p>

      {/* Cross-surface collision guard. beginRecording sets collisionOpen
          when always-on is already capturing; the user must stop that
          surface first and re-click Record. */}
      <ConfirmModal
        isOpen={collisionOpen}
        title="Always-on is already recording"
        description={(
          <>
            Always-on capture is currently running. Stop it from the
            Always-on panel, then click Record again.
          </>
        )}
        confirmLabel="Got it"
        cancelLabel="Cancel"
        tone="danger"
        onConfirm={() => setCollisionOpen(false)}
        onCancel={() => setCollisionOpen(false)}
      />
    </div>
  );
}
