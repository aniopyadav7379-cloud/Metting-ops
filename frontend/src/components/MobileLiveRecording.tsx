/**
 * Mobile recording surface.
 *
 * Phase B (v0.7.5): migrated from the one-shot blob-at-stop pipeline to
 * the same chunked-upload path desktop has used since v0.7.4. Audio is
 * captured by MediaRecorder and streamed to the server in 30s chunks
 * (`/api/recordings/sessions/{id}/audio-chunks`) with exponential-backoff
 * retries (max 5). On Stop we flush, drain the upload queue, then POST
 * `/finalize` + `/finalize-audio` to kick the server-side reprocess
 * pipeline (Parakeet 1.1B fp16 -> pyannote -> Qwen 3.6 35B-A3B-Vision).
 *
 * Why this matters on mobile: the old path held the entire recording in
 * memory and uploaded as one blob at Stop. A backgrounded tab killed by
 * iOS, a battery-saver-triggered Safari reset, or a flaky network at
 * Stop time = total loss of the recording. With chunked upload the user
 * can lose the *last* 30 seconds at worst.
 *
 * iOS Safari specifics handled here:
 *   * MIME order in fullAudioRecorder.ts probes audio/mp4 first on
 *     WebKit family (Safari 18.4+ also exposes WebM, but MP4 is the
 *     years-proven path).
 *   * Screen Wake Lock acquired while recording (Safari 18.4+ PWAs).
 *   * visibilitychange surfaces a "tab backgrounded" hint — iOS can
 *     suspend MediaRecorder in a background tab, so we warn the user
 *     instead of silently losing audio.
 *   * Capture-only banner from Phase A is preserved.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Mic,
  Square,
  ChevronUp,
  X,
  AlertCircle,
  Loader2,
  CheckCircle2,
  Smartphone,
  CloudUpload,
  EyeOff,
} from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';
import { useTierFeatures } from '../hooks/useTierFeatures';
import { Dialog } from './ui/Dialog';
import {
  isLikelyTouchDevice,
  tapAudioLevel,
  unlockAudioContext,
} from '../utils/mobileRecording';
import {
  getPreferredDeviceId,
  openMicrophoneStream,
} from '../utils/audioDevicePreference';
import {
  isAlwaysOnActive,
  setRecordActive,
} from '../utils/recordingPresence';
import {
  createFullAudioRecorder,
  pickRecordableMime,
  type FullAudioRecorderHandle,
} from '../utils/fullAudioRecorder';
import ConfirmModal from './ConfirmModal';

type RecorderState =
  | 'idle'
  | 'preparing'
  | 'recording'
  | 'finalizing'
  | 'reprocess-kicked'
  | 'done';

const TITLE_STORAGE_KEY = 'meetingops.mobile.title.v1';
const ACTIVE_RECORDING_KEY = 'meetingops.mobile.activeRecording.v1';

function formatDuration(seconds: number): string {
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

/** Minimal Wake Lock wrapper — Safari 18.4+ in installed PWAs exposes
 *  this, as do all modern Chromium/Firefox builds. We never throw if
 *  it's unavailable; the recording works either way, the screen just
 *  goes to sleep faster. */
interface WakeLockHandle {
  release: () => void;
}

async function requestWakeLock(): Promise<WakeLockHandle | null> {
  try {
    const nav = navigator as any;
    if (!nav?.wakeLock?.request) return null;
    const sentinel = await nav.wakeLock.request('screen');
    let released = false;
    const onRelease = () => {
      released = true;
    };
    sentinel.addEventListener?.('release', onRelease);
    return {
      release: () => {
        if (released) return;
        try {
          sentinel.release?.().catch(() => undefined);
        } catch {
          /* noop */
        }
      },
    };
  } catch {
    // User-gesture missing, permission denied, or browser doesn't
    // support it. Recording continues either way.
    return null;
  }
}

export default function MobileLiveRecording() {
  const { activeOrganization } = useOrg();
  // v3.19 moat fix. Mobile is paid-only until on-device mobile STT +
  // summary ships (phones can't run Parakeet 0.6B + Qwen 3 0.6B
  // reliably). Free users hit a full-screen gate before the recorder
  // ever opens a mic stream or hits `/api/recordings/start-always-on`,
  // so the "browser-only, never leaves device" free-tier promise stays
  // intact on the device most signups arrive from. See audit
  // 2026-05-29 §1 (MobileLiveRecording.tsx:4,315).
  const { hasFeature } = useTierFeatures();
  // `canonical_reprocess` is the right capability — it gates the
  // server-side completion pass (Parakeet 1.1B + pyannote + Qwen 3.6)
  // that every chunk we'd upload from this page feeds. Backend mirrors
  // this gate in `auth/tier.py`; the UI just refuses to start the
  // capture so we don't leak the user's mic audio to the server before
  // the 403 lands.
  const canUseMobileServer = hasFeature('canonical_reprocess');
  const [freeTierGateOpen, setFreeTierGateOpen] = useState(false);
  // Legal: pre-record consent gate (AUP §2.3). Shown once per browser session
  // before the mic opens; confirmation is remembered client-side.
  const [state, setState] = useState<RecorderState>('idle');
  const [title, setTitle] = useState<string>(() => {
    const stored = localStorage.getItem(TITLE_STORAGE_KEY);
    if (stored) return stored;
    const now = new Date();
    return `Meeting ${now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`;
  });
  const [duration, setDuration] = useState(0);
  const [audioLevel, setAudioLevel] = useState(0);
  const [moreOpen, setMoreOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [chunksUploaded, setChunksUploaded] = useState(0);
  const [chunksFailed, setChunksFailed] = useState(0);
  const [chunksProduced, setChunksProduced] = useState(0);
  // Whether the server reprocess pass was actually accepted — so the "done"
  // screen doesn't claim "Server is processing" when the kick-off failed.
  const [reprocessKicked, setReprocessKicked] = useState(false);
  // Surfaced when iOS Safari throws our tab into the background, which
  // can suspend MediaRecorder. We don't try to recover — we just warn.
  const [backgrounded, setBackgrounded] = useState(false);
  // Collision-guard prompt — surfaces when the user taps Record while
  // always-on is capturing. We refuse to start until they confirm + stop
  // always-on from its own surface.
  const [collisionOpen, setCollisionOpen] = useState(false);
  // Selected recordable MIME — looked up once on mount. Used both to
  // sanity-check capability and to surface in the More panel.
  const [selectedMime, setSelectedMime] = useState<string>('');

  useEffect(() => {
    setSelectedMime(pickRecordableMime());
  }, []);

  // Stable refs for hot paths.
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const recorderRef = useRef<FullAudioRecorderHandle | null>(null);
  const startTimeRef = useRef<number | null>(null);
  const tickRef = useRef<number | null>(null);
  const heartbeatRef = useRef<number | null>(null);
  const levelTapRef = useRef<{ stop: () => void } | null>(null);
  const wakeLockRef = useRef<WakeLockHandle | null>(null);
  const transcriptScrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    localStorage.setItem(TITLE_STORAGE_KEY, title);
  }, [title]);

  useEffect(() => {
    if (state !== 'recording') return;
    const id = window.setInterval(() => {
      if (startTimeRef.current) {
        setDuration(Math.floor((Date.now() - startTimeRef.current) / 1000));
      }
    }, 500);
    tickRef.current = id;
    return () => {
      window.clearInterval(id);
      tickRef.current = null;
    };
  }, [state]);

  // visibilitychange + pagehide: surface a warning + tighten Wake Lock
  // re-acquire on resume. iOS Safari can suspend a MediaRecorder when
  // the tab is hidden; we can't prevent that from JS but we can be
  // honest to the user so they know to keep the screen on.
  useEffect(() => {
    if (state !== 'recording') return;
    const onVisibility = () => {
      if (typeof document === 'undefined') return;
      const hidden = document.visibilityState !== 'visible';
      setBackgrounded(hidden);
      if (!hidden) {
        // Re-acquire wake lock — it's released on hide on most platforms.
        void requestWakeLock().then((handle) => {
          if (wakeLockRef.current) wakeLockRef.current.release();
          wakeLockRef.current = handle;
        });
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [state]);

  const releaseWakeLock = useCallback(() => {
    if (wakeLockRef.current) {
      try {
        wakeLockRef.current.release();
      } catch {
        /* noop */
      }
      wakeLockRef.current = null;
    }
  }, []);

  const cleanupCapture = useCallback(() => {
    // recorder.stop() is async; in cleanup paths we fire and forget.
    if (recorderRef.current) {
      try {
        recorderRef.current.stop().catch(() => undefined);
      } catch {
        /* noop */
      }
      recorderRef.current = null;
    }
    if (heartbeatRef.current !== null) {
      window.clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => undefined);
      audioCtxRef.current = null;
    }
    if (levelTapRef.current) {
      levelTapRef.current.stop();
      levelTapRef.current = null;
    }
    setAudioLevel(0);
    releaseWakeLock();
    // Cross-surface presence flag — clear here so error/unmount paths
    // free always-on. Idempotent.
    setRecordActive(false);
  }, [releaseWakeLock]);

  useEffect(() => () => cleanupCapture(), [cleanupCapture]);

  const pendingUploads = Math.max(0, chunksProduced - chunksUploaded - chunksFailed);

  const beginRecording = useCallback(async () => {
    setError(null);
    setPermissionDenied(false);
    setChunksProduced(0);
    setChunksUploaded(0);
    setChunksFailed(0);
    setBackgrounded(false);

    // v3.19 free-tier moat gate. Mobile recording is paid-only until
    // on-device mobile STT ships — we bail BEFORE opening the mic so
    // free-user audio never leaves the device, even momentarily, when
    // we're going to refuse to upload it anyway. Surfaces a dedicated
    // dialog explaining the constraint + pointing at desktop free mode.
    if (!canUseMobileServer) {
      setFreeTierGateOpen(true);
      return;
    }

    // Cross-surface guard. If always-on is already capturing audio,
    // prompt the user instead of silently starting a second capture.
    if (isAlwaysOnActive()) {
      setCollisionOpen(true);
      return;
    }

    setState('preparing');
    unlockAudioContext();

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('This browser cannot capture audio. Update to the latest Safari or Chrome.');
      setState('idle');
      return;
    }

    if (!activeOrganization) {
      setError('No active organization selected. Open Settings, pick an org, then try again.');
      setState('idle');
      return;
    }

    // Quick capability probe so we fail fast if MediaRecorder is
    // missing entirely (very old Safari, jsdom in tests).
    const probedMime = pickRecordableMime();
    if (!probedMime) {
      setError('This browser does not expose a usable audio recorder. Update Safari/Chrome.');
      setState('idle');
      return;
    }
    setSelectedMime(probedMime);

    // 1. Open the mic. Tab capture is desktop-Chromium-only and pointless
    //    on a phone, so we don't offer it here.
    let micStream: MediaStream | null = null;
    try {
      const opened = await openMicrophoneStream(getPreferredDeviceId(), {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      });
      micStream = opened.stream;
    } catch (err: any) {
      const denied = err?.name === 'NotAllowedError' || err?.name === 'SecurityError';
      setPermissionDenied(denied);
      setError(
        denied
          ? 'Microphone access was blocked. Allow microphone permission in your browser, then try again.'
          : `Could not start the microphone: ${err?.message ?? err}`,
      );
      setState('idle');
      return;
    }

    streamRef.current = micStream;
    levelTapRef.current = tapAudioLevel(micStream, setAudioLevel);

    // 2. Create the always-on session so /audio-chunks accepts our POSTs.
    //    Always-on is the required mode for the chunked-audio endpoint;
    //    this is the same endpoint AlwaysOnContext uses on desktop.
    let createdSessionId: string;
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (activeOrganization.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
      const isIOSUA = /iPhone|iPad|iPod/.test(navigator.userAgent || '');
      const resp = await fetch(`${config.apiUrl}/api/recordings/start-always-on`, {
        method: 'POST',
        headers,
        credentials: 'include',
        body: JSON.stringify({
          org_id: activeOrganization.id,
          source_label: `mobile:${isIOSUA ? 'iOS' : 'Mobile'}`,
        }),
      });
      if (!resp.ok) {
        const detail = await resp.text().catch(() => '');
        throw new Error(detail || `Could not start session (${resp.status})`);
      }
      const payload = await resp.json();
      createdSessionId = String(payload.session_id ?? payload.id);
      if (!createdSessionId || createdSessionId === 'undefined') {
        throw new Error('Server returned no session id.');
      }
    } catch (err: any) {
      setError(`Could not start the session: ${err?.message ?? err}`);
      cleanupCapture();
      setState('idle');
      return;
    }

    setSessionId(createdSessionId);

    // 3. Wire fullAudioRecorder against the mic stream. This is the
    //    same plumbing AlwaysOnContext uses on desktop — parallel
    //    MediaRecorder, sequential FIFO upload queue with retries,
    //    idempotent on chunk_index.
    const recorder = createFullAudioRecorder({
      stream: micStream,
      sessionId: createdSessionId,
      apiUrl: config.apiUrl,
      orgSlug: activeOrganization.slug ?? null,
      timesliceMs: 30_000,
      maxRetries: 5,
      initialBackoffMs: 1000,
      onChunkUploaded: () => {
        setChunksUploaded((n) => n + 1);
      },
      onChunkFailed: (idx, err) => {
        // eslint-disable-next-line no-console
        console.warn(`[mobile-rec] chunk ${idx} dropped after retries:`, err.message);
        setChunksFailed((n) => n + 1);
      },
    });
    if (!recorder) {
      setError('Could not initialize the audio recorder. Try a different browser.');
      cleanupCapture();
      setState('idle');
      return;
    }
    recorderRef.current = recorder;
    recorder.start();

    // Heartbeat: track how many chunks the recorder has produced (regardless
    // of upload state). Gives the user a real-time queue depth display.
    const heartbeat = window.setInterval(() => {
      const handle = recorderRef.current;
      if (!handle) {
        if (heartbeatRef.current !== null) {
          window.clearInterval(heartbeatRef.current);
          heartbeatRef.current = null;
        }
        return;
      }
      setChunksProduced(handle.chunksProduced());
    }, 1000);
    heartbeatRef.current = heartbeat;

    // 4. Acquire screen wake lock so the user doesn't get auto-locked
    //    during a long meeting. iOS 18.4+ PWAs honor this; older
    //    versions silently no-op.
    wakeLockRef.current = await requestWakeLock();

    startTimeRef.current = Date.now();
    setDuration(0);
    setState('recording');
    // Flip the cross-surface presence flag — always-on refuses to start
    // while this is true. cleanupCapture + finishRecording clear it.
    setRecordActive(true);

    localStorage.setItem(
      ACTIVE_RECORDING_KEY,
      JSON.stringify({
        startedAt: startTimeRef.current,
        title,
        sessionId: createdSessionId,
        mimeType: probedMime,
      }),
    );
  }, [activeOrganization, canUseMobileServer, cleanupCapture, title]);

  const finishRecording = useCallback(async () => {
    if (state !== 'recording') return;
    setState('finalizing');

    const recorder = recorderRef.current;
    const localSessionId = sessionId;
    recorderRef.current = null;

    // Stop the per-second heartbeat first so we don't fight with the
    // final chunksProduced count.
    if (heartbeatRef.current !== null) {
      window.clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }

    if (recorder) {
      // 1. Flush whatever is in the encoder right now into the queue.
      try {
        await recorder.flush();
      } catch {
        /* best effort */
      }
      // 2. Stop the recorder and wait for the upload queue to drain.
      //    `stop()` resolves only after the chained sequential uploads
      //    complete (or hit max retries), so when this await returns
      //    we know every chunk has been ACKed (or marked failed).
      try {
        await recorder.stop();
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('[mobile-rec] recorder.stop() warning', err);
      }
    }

    // Final tally for the UI before we transition away.
    if (recorder) setChunksProduced(recorder.chunksProduced());

    // 3. Tear down audio plumbing (mic tracks, audio context, level tap,
    //    wake lock). Session lifecycle continues server-side.
    cleanupCapture();
    localStorage.removeItem(ACTIVE_RECORDING_KEY);

    if (!localSessionId) {
      setError('Session id missing — recording was not associated with a server record.');
      setState('idle');
      return;
    }

    // 4. POST /finalize -> /finalize-audio. Finalize transitions the
    //    session from `recording` to `processing` so the audio-chunks
    //    endpoint stops accepting late chunks; finalize-audio queues
    //    the ffmpeg reassemble + Parakeet 1.1B + pyannote + Qwen 3.6
    //    pipeline as a BackgroundTask. Both calls are idempotent; we
    //    surface the error if either fails but don't block the user
    //    from leaving the screen.
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
    try {
      const finalizeResp = await fetch(
        `${config.apiUrl}/api/recordings/sessions/${localSessionId}/finalize`,
        { method: 'POST', headers, credentials: 'include' },
      );
      if (!finalizeResp.ok) {
        const detail = await finalizeResp.text().catch(() => '');
        throw new Error(detail || `Finalize failed (${finalizeResp.status})`);
      }
    } catch (err: any) {
      // eslint-disable-next-line no-console
      console.warn('[mobile-rec] /finalize failed', err);
      setError(`Finalize hit an error: ${err?.message ?? err}. The audio is on the server; you can reprocess from Sessions if needed.`);
      setState('done');
      return;
    }

    let kicked = false;
    try {
      const reprocResp = await fetch(
        `${config.apiUrl}/api/recordings/sessions/${localSessionId}/finalize-audio`,
        { method: 'POST', headers, credentials: 'include' },
      );
      if (reprocResp.ok) {
        kicked = true;
      } else {
        // Don't claim "Server is processing" — the kick-off was rejected.
        // eslint-disable-next-line no-console
        console.warn('[mobile-rec] /finalize-audio non-ok', reprocResp.status);
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[mobile-rec] /finalize-audio kick-off failed', err);
    }

    setReprocessKicked(kicked);
    setState('reprocess-kicked');
    // Small dwell so the user sees the confirmation, then move to the
    // final "done" state. They can navigate to Sessions whenever.
    window.setTimeout(() => setState('done'), 1200);
  }, [activeOrganization, cleanupCapture, sessionId, state]);

  const resetForNext = useCallback(() => {
    setState('idle');
    setDuration(0);
    setSessionId(null);
    setChunksUploaded(0);
    setChunksProduced(0);
    setChunksFailed(0);
    // Refresh title default for next meeting.
    const now = new Date();
    setTitle(
      `Meeting ${now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`,
    );
  }, []);

  const handlePrimaryTap = useCallback(() => {
    if (state === 'idle') {
      void beginRecording();
    } else if (state === 'recording') {
      void finishRecording();
    } else if (state === 'done') {
      resetForNext();
    }
  }, [beginRecording, finishRecording, resetForNext, state]);

  const isBusy = state === 'preparing' || state === 'finalizing' || state === 'reprocess-kicked';
  const buttonLabel = state === 'idle'
    ? 'Tap to record'
    : state === 'preparing'
      ? 'Starting mic...'
      : state === 'recording'
        ? 'Tap to stop'
        : state === 'finalizing'
          ? 'Uploading last chunk...'
          : state === 'reprocess-kicked'
            ? 'Sending to processing...'
            : 'Record another';

  const supportsRecording = isLikelyTouchDevice() && typeof MediaRecorder !== 'undefined';

  const helperText = !supportsRecording
    ? 'This phone does not support in-browser recording. Use the desktop layout, or upload an existing audio file from Sessions.'
    : error
      ? error
      : 'Audio uploads in small chunks while you record, so the meeting is safe even if Safari is killed. Keep this tab in the foreground and the screen unlocked.';

  const uploadIndicator = useMemo(() => {
    if (state !== 'recording' && state !== 'finalizing') return null;
    if (chunksProduced === 0 && state === 'recording') {
      return {
        tone: 'pending' as const,
        text: 'First chunk lands in about 30 seconds...',
      };
    }
    if (chunksFailed > 0) {
      return {
        tone: 'warn' as const,
        text: `${chunksUploaded} uploaded, ${chunksFailed} retry exhausted, ${pendingUploads} pending`,
      };
    }
    if (pendingUploads > 0) {
      return {
        tone: 'pending' as const,
        text: `${chunksUploaded} uploaded, ${pendingUploads} queued`,
      };
    }
    return {
      tone: 'ok' as const,
      text: `${chunksUploaded} chunk${chunksUploaded === 1 ? '' : 's'} uploaded`,
    };
  }, [chunksFailed, chunksProduced, chunksUploaded, pendingUploads, state]);

  return (
    <div className="flex flex-col bg-zinc-950 text-zinc-100 min-h-screen md:hidden"
         style={{
           paddingTop: 'env(safe-area-inset-top)',
           paddingLeft: 'env(safe-area-inset-left)',
           paddingRight: 'env(safe-area-inset-right)',
         }}>
      <div className="px-4 pt-4 pb-2 border-b border-zinc-800 flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-fuchsia-500 to-indigo-500 flex items-center justify-center shrink-0">
          <Mic className="w-4 h-4 text-white" />
        </div>
        <input
          aria-label="Meeting title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          disabled={state === 'recording' || state === 'finalizing'}
          className="flex-1 bg-transparent text-base font-semibold text-white outline-none border-b border-transparent focus:border-fuchsia-500 transition-colors py-1 disabled:text-zinc-400 disabled:cursor-not-allowed"
          placeholder="Meeting title"
        />
      </div>

      <div className="px-4 pt-4 pb-3 flex flex-col gap-3">
        <div className="flex items-baseline justify-between">
          <div className="text-3xl font-mono tabular-nums text-white">{formatDuration(duration)}</div>
          <div className="text-xs uppercase tracking-wider text-zinc-400">
            {state === 'recording' ? (
              <span className="text-red-300 flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" /> Live
              </span>
            ) : state === 'preparing' ? 'Starting'
              : state === 'finalizing' ? 'Finalizing'
                : state === 'reprocess-kicked' ? 'Processing'
                  : state === 'done' ? 'Done'
                    : 'Ready'}
          </div>
        </div>

        <div className="h-3 rounded-full bg-zinc-800 overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-emerald-500 via-fuchsia-500 to-indigo-500 transition-[width] duration-100"
            style={{ width: `${Math.round(Math.min(1, audioLevel) * 100)}%` }}
          />
        </div>

        {uploadIndicator && (
          <div className={`rounded-xl border px-3 py-2 flex items-center gap-3 text-xs ${
            uploadIndicator.tone === 'warn'
              ? 'bg-amber-500/5 border-amber-500/30 text-amber-100'
              : uploadIndicator.tone === 'ok'
                ? 'bg-emerald-500/5 border-emerald-500/30 text-emerald-100'
                : 'bg-zinc-900/80 border-zinc-800 text-zinc-200'
          }`}>
            <CloudUpload className={`w-4 h-4 shrink-0 ${uploadIndicator.tone === 'warn' ? 'text-amber-300' : uploadIndicator.tone === 'ok' ? 'text-emerald-300' : 'text-fuchsia-300'}`} />
            <span className="flex-1">{uploadIndicator.text}</span>
          </div>
        )}

        {backgrounded && state === 'recording' && (
          <div className="rounded-xl bg-amber-500/10 border border-amber-500/30 px-3 py-2 flex items-start gap-3 text-xs text-amber-100">
            <EyeOff className="w-4 h-4 shrink-0 mt-0.5" />
            <div>
              <div className="font-medium">Tab is in the background</div>
              <div className="text-amber-200/80">iOS Safari can pause recording when this tab is hidden. Return to the tab to keep capturing.</div>
            </div>
          </div>
        )}
      </div>

      <div
        ref={transcriptScrollRef}
        className="flex-1 overflow-y-auto px-4 pb-40 pt-2 space-y-2 text-sm leading-relaxed"
      >
        {/* Phase A mobile gate explainer. The live-transcript and
            live-summary panes are intentionally absent on mobile —
            phones can't run Parakeet 0.6B or the in-browser LLM well
            enough to be honest about it. We frame this as a deliberate
            mode, not a missing feature. */}
        <div className="rounded-xl bg-sky-500/5 border border-sky-500/30 p-4 text-sm">
          <div className="flex items-start gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-sky-500/20 text-sky-300">
              <Smartphone className="h-4 w-4" />
            </div>
            <div>
              <div className="font-medium text-sky-100 mb-1">Capture-only mode</div>
              <div className="text-zinc-300">
                Your device captures audio and uploads it as you record.
                The full transcript and AI summary will be ready about a
                minute after the meeting ends, on the session page.
              </div>
            </div>
          </div>
        </div>

        {state === 'done' ? (
          <div className={`rounded-xl p-4 text-sm border ${reprocessKicked ? 'bg-emerald-500/5 border-emerald-500/30' : 'bg-amber-500/5 border-amber-500/30'}`}>
            <div className="flex items-start gap-3">
              <CheckCircle2 className={`w-5 h-5 shrink-0 mt-0.5 ${reprocessKicked ? 'text-emerald-300' : 'text-amber-300'}`} />
              <div>
                <div className={`font-medium mb-1 ${reprocessKicked ? 'text-emerald-100' : 'text-amber-100'}`}>
                  {reprocessKicked ? 'Done. Server is processing.' : 'Audio saved — processing not started.'}
                </div>
                <div className="text-zinc-300">
                  {reprocessKicked
                    ? 'Audio is on the server. The full quality pass (Parakeet 1.1B, speaker diarization, Qwen 3.6 summary) runs now and lands on the session page in about a minute.'
                    : "Your audio is safely on the server, but the processing pass didn't start. Open the session and tap Reprocess to generate the transcript + summary."}
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <a
                    href="#/sessions"
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-500/20 border border-emerald-500/40 text-emerald-100 active:bg-emerald-500/30"
                  >
                    Open Sessions
                  </a>
                  {chunksFailed > 0 && (
                    <span className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-100">
                      {chunksFailed} chunk{chunksFailed === 1 ? '' : 's'} dropped after retries
                    </span>
                  )}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="rounded-xl bg-zinc-900/60 border border-zinc-800 p-4 text-zinc-400 text-sm">
            <div className="font-medium text-zinc-200 mb-1">
              {state === 'recording' ? 'Listening...' : state === 'finalizing' ? 'Finalizing...' : 'Ready when you are'}
            </div>
            <div>{helperText}</div>
          </div>
        )}
      </div>

      <div
        className="fixed bottom-0 left-0 right-0 z-30 px-4 pt-3 bg-zinc-950/90 backdrop-blur border-t border-zinc-800"
        style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 12px)' }}
      >
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setMoreOpen(true)}
            className="h-16 min-w-16 px-4 rounded-2xl bg-zinc-900 border border-zinc-800 text-zinc-200 active:bg-zinc-800 transition-colors flex items-center justify-center gap-2"
            aria-label="More controls"
          >
            <ChevronUp className="w-5 h-5" />
            <span className="text-xs">More</span>
          </button>
          <button
            type="button"
            onClick={handlePrimaryTap}
            disabled={isBusy || !supportsRecording}
            className={`flex-1 min-h-[64px] h-16 rounded-2xl text-white font-semibold text-base flex items-center justify-center gap-2 active:scale-[0.98] transition-all shadow-lg shadow-fuchsia-900/30 ${
              state === 'recording'
                ? 'bg-red-600 active:bg-red-500'
                : state === 'done'
                  ? 'bg-emerald-600 active:bg-emerald-500'
                  : 'bg-gradient-to-r from-fuchsia-600 to-indigo-600 active:from-fuchsia-500 active:to-indigo-500'
            } ${(isBusy || !supportsRecording) ? 'opacity-60' : ''}`}
            aria-pressed={state === 'recording'}
          >
            {state === 'recording'
              ? <Square className="w-6 h-6" fill="currentColor" />
              : state === 'finalizing' || state === 'reprocess-kicked' || state === 'preparing'
                ? <Loader2 className="w-6 h-6 animate-spin" />
                : <Mic className="w-6 h-6" />}
            <span>{buttonLabel}</span>
          </button>
        </div>
        {permissionDenied && (
          <div className="mt-3 rounded-lg bg-red-900/30 border border-red-800/60 px-3 py-2 text-xs text-red-200 flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}
      </div>

      {moreOpen && (
        <div className="fixed inset-0 z-40 flex flex-col">
          <button
            type="button"
            className="flex-1 bg-black/60"
            onClick={() => setMoreOpen(false)}
            aria-label="Close panel"
          />
          <div
            className="bg-zinc-950 border-t border-zinc-800 rounded-t-3xl px-5 pt-4"
            style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 16px)' }}
          >
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-white font-semibold text-base">Recording options</h2>
              <button
                type="button"
                onClick={() => setMoreOpen(false)}
                className="w-9 h-9 rounded-full bg-zinc-900 border border-zinc-800 flex items-center justify-center text-zinc-300"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="space-y-4 text-sm text-zinc-300">
              <div>
                <div className="text-zinc-400 text-xs uppercase tracking-wider mb-1">Audio source</div>
                <div className="text-zinc-200">Phone microphone</div>
                <div className="text-xs text-zinc-500 mt-1">
                  Tab capture is desktop-only. On iOS the mic is the only available source; AirPods or Bluetooth headsets work if they are paired before tapping Record.
                </div>
              </div>
              <div>
                <div className="text-zinc-400 text-xs uppercase tracking-wider mb-1">Audio format</div>
                <div className="text-zinc-200">
                  {selectedMime || 'Auto-selected at record time'}
                </div>
                <div className="text-xs text-zinc-500 mt-1">
                  Safari uses audio/mp4 (AAC); other browsers use audio/webm (Opus). The server transcodes either way.
                </div>
              </div>
              <div>
                <div className="text-zinc-400 text-xs uppercase tracking-wider mb-1">Upload</div>
                <div className="text-zinc-200">
                  Chunked, every 30 seconds, with retry. A crashed browser loses at most the last chunk.
                </div>
              </div>
              <div>
                <div className="text-zinc-400 text-xs uppercase tracking-wider mb-1">Install</div>
                <div className="text-zinc-200">
                  Tap the Share icon, then "Add to Home Screen" to launch Meet-Ops without the Safari chrome. Wake Lock (screen stays on) requires the installed PWA on iOS.
                </div>
              </div>
              <div>
                <div className="text-zinc-400 text-xs uppercase tracking-wider mb-1">Sessions</div>
                <a
                  href="#/sessions"
                  className="inline-flex items-center px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-zinc-200 active:bg-zinc-800"
                  onClick={() => setMoreOpen(false)}
                >
                  View completed sessions
                </a>
              </div>
              {state === 'recording' && (
                <div className="rounded-lg bg-zinc-900/60 border border-zinc-800 p-3">
                  <div className="text-zinc-400 text-xs uppercase tracking-wider mb-1">Diagnostics</div>
                  <dl className="space-y-1 text-xs text-zinc-300">
                    <div className="flex justify-between"><dt>Session</dt><dd className="font-mono">{sessionId?.slice(0, 8) ?? '-'}</dd></div>
                    <div className="flex justify-between"><dt>Chunks produced</dt><dd>{chunksProduced}</dd></div>
                    <div className="flex justify-between"><dt>Uploaded</dt><dd>{chunksUploaded}</dd></div>
                    <div className="flex justify-between"><dt>Pending</dt><dd>{pendingUploads}</dd></div>
                    <div className="flex justify-between"><dt>Retry exhausted</dt><dd className={chunksFailed > 0 ? 'text-amber-300' : ''}>{chunksFailed}</dd></div>
                  </dl>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Cross-surface collision guard for mobile. Mirrors the desktop
          flow: refuses to start a tap-Record while always-on is live. */}
      <ConfirmModal
        isOpen={collisionOpen}
        title="Always-on is already recording"
        description={(
          <>
            Always-on capture is currently running. Stop it from the
            Always-on panel, then tap Record again.
          </>
        )}
        confirmLabel="Got it"
        cancelLabel="Cancel"
        tone="danger"
        onConfirm={() => setCollisionOpen(false)}
        onCancel={() => setCollisionOpen(false)}
      />

      {/* v3.19 free-tier moat gate. Triggered the moment a free user
          taps Record on mobile, BEFORE the mic stream opens or any
          chunk hits the server. The copy is intentionally honest about
          *why* mobile is paid-only (phones can't run the on-device
          models reliably yet) — see audit 2026-05-29 §1 + §5. */}
      <Dialog
        open={freeTierGateOpen}
        onOpenChange={(open) => setFreeTierGateOpen(open)}
      >
        <Dialog.Content
          size="lg"
          describedById="mobile-free-tier-gate-description"
        >
          <Dialog.Header>
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-500/15 text-sky-300">
                <Smartphone className="h-4 w-4" />
              </div>
              <Dialog.Title>Mobile recording is a Pro feature</Dialog.Title>
            </div>
            <Dialog.Close />
          </Dialog.Header>
          <Dialog.Description
            id="mobile-free-tier-gate-description"
            className="px-5 py-4 text-sm leading-6 text-zinc-200"
            asChild
          >
            <div>
              Free mobile browser recording is not yet available because
              phones can&apos;t reliably run the local speech and summary
              models. Use desktop Chrome or Edge for free local
              recording, or upgrade to Pro to use mobile server
              completion.
            </div>
          </Dialog.Description>
          <Dialog.Footer className="flex-col-reverse items-stretch gap-2 sm:flex-row sm:items-center sm:justify-end">
            <a
              href="#/help"
              onClick={() => setFreeTierGateOpen(false)}
              className="inline-flex items-center justify-center rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-100 transition hover:bg-zinc-700"
            >
              Use desktop free mode
            </a>
            <a
              href="#/pricing"
              onClick={() => setFreeTierGateOpen(false)}
              className="inline-flex items-center justify-center rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:from-fuchsia-500 hover:to-indigo-500"
            >
              View Pro pricing
            </a>
          </Dialog.Footer>
        </Dialog.Content>
      </Dialog>

      {/* Legal: pre-record consent gate (AUP §2.3). Confirming records consent
          for the session (client-side v1) and resumes the recording start. */}
    </div>
  );
}
