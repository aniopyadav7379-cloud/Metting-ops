import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, Mic, Play, Square } from 'lucide-react';
import AudioMeter from '../AudioMeter';
import { openMicrophoneStream } from '../../utils/audioDevicePreference';

/**
 * "Test microphone" UI: records ~3 seconds from the user's preferred input,
 * then plays back. Shows a live VU meter during recording. Designed for
 * the AudioSettings panel — pre-flight check for "does this mic actually
 * work" before starting a real session.
 *
 * State machine:
 *   idle → recording (3s countdown, VU meter live) → playing → idle
 *
 * Recording uses MediaRecorder with the mime type the browser supports
 * best (webm/opus on Chromium, mp4/aac on Safari). The recorded Blob is
 * played back via a hidden <audio> element to keep the surface tiny.
 */

interface MicTestButtonProps {
  /** Browser deviceId to test. null = system default. */
  deviceId: string | null;
  /** Optional fixed duration in ms; defaults to 3000. */
  durationMs?: number;
}

type TestState = 'idle' | 'preparing' | 'recording' | 'playing' | 'failed';

function pickMimeType(): string {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
    'audio/ogg',
  ];
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) {
    return '';
  }
  for (const type of candidates) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return '';
}

export default function MicTestButton({
  deviceId,
  durationMs = 3000,
}: MicTestButtonProps) {
  const [state, setState] = useState<TestState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [remainingMs, setRemainingMs] = useState<number>(durationMs);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stopTimerRef = useRef<number | null>(null);
  const countdownRef = useRef<number | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  const cleanupRecording = useCallback(() => {
    if (stopTimerRef.current !== null) {
      window.clearTimeout(stopTimerRef.current);
      stopTimerRef.current = null;
    }
    if (countdownRef.current !== null) {
      window.clearInterval(countdownRef.current);
      countdownRef.current = null;
    }
    const s = streamRef.current;
    if (s) {
      s.getTracks().forEach((t) => t.stop());
    }
    streamRef.current = null;
    setStream(null);
    recorderRef.current = null;
  }, []);

  const stopTest = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
    }
    cleanupRecording();
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
    setState('idle');
    setRemainingMs(durationMs);
  }, [cleanupRecording, durationMs]);

  useEffect(() => () => stopTest(), [stopTest]);

  const startTest = useCallback(async () => {
    setError(null);
    setState('preparing');
    setRemainingMs(durationMs);
    chunksRef.current = [];

    let micStream: MediaStream;
    try {
      const opened = await openMicrophoneStream(deviceId);
      micStream = opened.stream;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setError(`Could not open microphone: ${message}`);
      setState('failed');
      return;
    }
    streamRef.current = micStream;
    setStream(micStream);

    const mimeType = pickMimeType();
    let recorder: MediaRecorder;
    try {
      recorder = mimeType
        ? new MediaRecorder(micStream, { mimeType })
        : new MediaRecorder(micStream);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setError(`MediaRecorder unavailable: ${message}`);
      cleanupRecording();
      setState('failed');
      return;
    }
    recorderRef.current = recorder;

    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onstop = () => {
      cleanupRecording();
      const blob = new Blob(chunksRef.current, {
        type: mimeType || 'audio/webm',
      });
      if (blob.size === 0) {
        setError('No audio captured. Make sure the mic is unmuted.');
        setState('failed');
        return;
      }
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = URL.createObjectURL(blob);
      const audio = audioRef.current;
      if (audio) {
        audio.src = objectUrlRef.current;
        audio.onended = () => {
          setState('idle');
          if (objectUrlRef.current) {
            URL.revokeObjectURL(objectUrlRef.current);
            objectUrlRef.current = null;
          }
        };
        setState('playing');
        audio.play().catch((err) => {
          setError(`Playback failed: ${err?.message ?? err}`);
          setState('failed');
        });
      } else {
        setState('idle');
      }
    };

    recorder.start();
    setState('recording');

    // Countdown for the UI label
    const startedAt = Date.now();
    countdownRef.current = window.setInterval(() => {
      const remaining = Math.max(0, durationMs - (Date.now() - startedAt));
      setRemainingMs(remaining);
    }, 100);

    stopTimerRef.current = window.setTimeout(() => {
      try {
        if (recorderRef.current && recorderRef.current.state !== 'inactive') {
          recorderRef.current.stop();
        }
      } catch {
        // ignore
      }
    }, durationMs);
  }, [cleanupRecording, deviceId, durationMs]);

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3">
        {state === 'idle' || state === 'failed' ? (
          <button
            type="button"
            onClick={startTest}
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-100 transition hover:border-zinc-600 hover:bg-zinc-700"
          >
            <Mic className="h-4 w-4" />
            Test microphone
          </button>
        ) : state === 'preparing' ? (
          <button
            type="button"
            disabled
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm font-medium text-zinc-400"
          >
            <Loader2 className="h-4 w-4 animate-spin" />
            Opening mic…
          </button>
        ) : state === 'recording' ? (
          <button
            type="button"
            onClick={stopTest}
            className="inline-flex items-center gap-2 rounded-lg border border-red-500/50 bg-red-500/10 px-3 py-1.5 text-sm font-medium text-red-200 transition hover:bg-red-500/20"
          >
            <Square className="h-4 w-4" />
            Stop ({Math.ceil(remainingMs / 1000)}s)
          </button>
        ) : (
          <button
            type="button"
            onClick={stopTest}
            className="inline-flex items-center gap-2 rounded-lg border border-emerald-500/50 bg-emerald-500/10 px-3 py-1.5 text-sm font-medium text-emerald-200 transition hover:bg-emerald-500/20"
          >
            <Play className="h-4 w-4" />
            Playing…
          </button>
        )}
        {state === 'recording' && (
          <div className="flex-1">
            <AudioMeter stream={stream} compact showNumeric ariaLabel="Test mic level" />
          </div>
        )}
      </div>
      {error && <p className="text-xs text-red-300">{error}</p>}
      <audio ref={audioRef} className="hidden" />
    </div>
  );
}
