import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { config } from '../config';
import { setRecordingActive } from '../utils/installFetchInterceptor';
import { useOrg } from './OrgContext';
import type { AlwaysOnVadEngine, VadAudioChunk, VadStreamOpenResult } from '../utils/vadEngine';
import {
  getAutoPreferUsb,
  getPreferredDeviceId,
  isUsbAudioDevice,
  setPreferredDeviceId,
} from '../utils/audioDevicePreference';
import {
  AUDIO_SOURCE_MODE_EVENT,
  AUDIO_SOURCE_MODE_KEY,
  getAudioSourceMode,
  openAudioSourceStream,
  setAudioSourceMode,
  type AudioSourceMode,
  type OpenedAudioSource,
} from '../utils/audioSourceStream';
import {
  isAlwaysOnActive,
  isRecordActive,
  setAlwaysOnActive,
  beatAlwaysOnActive,
} from '../utils/recordingPresence';
import { showToast } from '../components/Toast';
import { toast as toastify } from 'react-toastify';
import {
  getLiveSummaryAutoConfig,
  getModelLabel,
  getStoredModelId,
  inBrowserLLM,
  LIVE_SUMMARY_THRESHOLDS,
  type LiveSummaryAutoConfig,
  type SummaryParagraph,
} from '../services/inBrowserLLM';
import {
  CLIENT_STT_PROVENANCE,
  inBrowserSTT,
} from '../services/inBrowserSTT';
import {
  getSTTMode,
  type SttMode,
} from '../services/inBrowserSTTSettings';
import {
  getLocalOnly,
  isPrivacyModeAvailable,
} from '../services/privacyMode';
import {
  appendChunk as appendLocalChunk,
  appendSlice as appendLocalSlice,
  createLocalSession,
  deleteLocalSession,
  endSession as endLocalSession,
  isLocalSessionId,
  markParakeetPassFailed,
  markParakeetPassRunning,
  saveParakeetPass,
  type LocalTranscriptChunk,
} from '../services/localSessionStore';
import {
  createFullAudioRecorder,
  postFinalizeAudioWithVerification,
  postFullAudio,
  type FullAudioRecorderHandle,
} from '../utils/fullAudioRecorder';
import {
  detectDevice,
  shouldRunBrowserInference,
  shouldRunFullLocalPass,
  shouldShowCaptureOnlyBanner,
  supportsLocalPersistence,
  type DeviceCapability,
} from '../utils/deviceDetection';
import {
  localAudioStore,
  type LocalAudioSessionMeta,
} from '../services/localAudioStore';
import { useTierFeatures } from '../hooks/useTierFeatures';
import { track } from '../utils/posthog';

// vad-web pulls an ONNX runtime. It must not be part of the authenticated
// shell merely because the recording provider is mounted there.
let vadEngineModulePromise: Promise<typeof import('../utils/vadEngine')> | null = null;

function loadVadEngineModule(): Promise<typeof import('../utils/vadEngine')> {
  if (!vadEngineModulePromise) {
    vadEngineModulePromise = import('../utils/vadEngine');
  }
  return vadEngineModulePromise;
}

// Localstorage key matches PipelineStatusPicker. Read at upload time so the
// user can toggle Diarization on/off mid-session and the next chunk reflects
// the change without restarting the engine.
const DIARIZATION_OFF_KEY = 'meetingops.pipeline.diarization.off.v1';

function getDiarizationOff(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(DIARIZATION_OFF_KEY) === '1';
  } catch {
    return false;
  }
}

export type AlwaysOnState = 'idle' | 'starting' | 'recording' | 'paused' | 'stopping';

export interface LiveTranscriptSegment {
  id: string;
  sessionId: string;
  text: string;
  speaker?: string | null;
  start?: number;
  end?: number;
  timestamp: string;
}

export interface AlwaysOnSummarySlice {
  id: string;
  text: string;
  createdAt: string;
  transcriptStart: number;
  transcriptEnd: number;
  model: string;
  modelLabel: string;
  durationMs: number;
}

export interface AlwaysOnSummary {
  slices: AlwaysOnSummarySlice[];
  currentDraft: string;
  lastWordCountAtSummary: number;
  /** Latest slice text, kept for backward compatibility with older callers. */
  text: string;
  paragraphs: SummaryParagraph[];
  updatedAt?: string;
}

const EMPTY_SUMMARY: AlwaysOnSummary = {
  slices: [],
  currentDraft: '',
  lastWordCountAtSummary: 0,
  text: '',
  paragraphs: [],
};

function uuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `slice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function countWords(text: string): number {
  if (!text) return 0;
  const matches = text.trim().match(/\S+/g);
  return matches ? matches.length : 0;
}

function thresholdSpec(threshold: LiveSummaryAutoConfig['threshold']) {
  return LIVE_SUMMARY_THRESHOLDS.find((t) => t.id === threshold) ?? LIVE_SUMMARY_THRESHOLDS[1];
}

export interface AlwaysOnContextValue {
  state: AlwaysOnState;
  vadActive: boolean;
  currentSessionId: string | null;
  chunksUploaded: number;
  elapsedSeconds: number;
  summary: AlwaysOnSummary;
  liveTranscript: LiveTranscriptSegment[];
  error: string | null;
  /**
   * Transient success notice shown after a clean Stop & save (e.g.
   * "Saved — processing your meeting."). Null when there's nothing to
   * confirm. Cleared on the next start()/restart() or via
   * dismissSaveConfirmation().
   */
  saveConfirmation: string | null;
  /** Dismiss the post-stop save confirmation banner. */
  dismissSaveConfirmation: () => void;
  paidFeaturePrompt: string | null;
  dismissPaidFeaturePrompt: () => void;
  statusText: string;
  sessionsCompleted: number;
  summarizing: boolean;
  modelLoadProgress: number | null;
  autoConfig: LiveSummaryAutoConfig;
  transcriptWordCount: number;
  sttMode: SttMode;
  sttReady: boolean;
  sttLoadProgress: number | null;
  sttLoadLabel: string | null;
  sttBrowserFailed: boolean;
  /** True when the current session is local-only (nothing leaves the device). */
  localOnly: boolean;
  /** True when WebGPU is missing and local-only would degrade to standard mode. */
  localOnlyForcedOff: boolean;
  /**
   * The deviceId we're actually capturing from right now. May differ from
   * the persisted preference if a fallback fired (e.g. plug-out). `null`
   * means "system default".
   */
  activeMicDeviceId: string | null;
  /**
   * The live `MediaStream` for the current capture. UI consumers can
   * attach AnalyserNodes to it for VU meters without re-prompting the
   * user for mic permission.
   */
  activeMicStream: MediaStream | null;
  /** True while a device swap is in flight (engine is restarting). */
  switchingDevice: boolean;
  /**
   * Switch to a different microphone mid-session. The persistent default
   * in localStorage is NOT touched — this is a per-session override.
   * To change the default, call setPreferredDeviceId() from
   * `utils/audioDevicePreference` instead.
   */
  switchMicDevice: (deviceId: string | null) => Promise<void>;
  /**
   * The audio source mode the user has picked: mic / tab / mic+tab.
   * Persisted in localStorage; mirrored here so the UI can react.
   * When mode != 'mic' the SessionMicChip + auto-prefer-USB are no-ops
   * for the duration of the session.
   */
  audioSourceMode: AudioSourceMode;
  /** Persist a new audio source mode. Only takes effect on next start. */
  setAudioSourceMode: (mode: AudioSourceMode) => void;
  /**
   * Swap the active audio source mid-session. Tears down the current
   * stream (mic, tab, or mixed AudioContext) and reopens via the new
   * mode through the engine's stream opener — same path switchMicDevice
   * uses, ~200ms gap. Also persists the new mode so the next start()
   * picks up where the user left off. No-op when idle.
   */
  switchAudioSource: (mode: AudioSourceMode) => Promise<void>;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  triggerSummarize: () => Promise<void>;
  /**
   * Hard-delete the current session. For server-backed sessions this
   * calls `DELETE /api/recordings/sessions/{id}` which cascades to
   * transcript rows + audio chunks on disk + qdrant points. For
   * local-only sessions we just drop the IndexedDB record. After the
   * delete the engine is reset to `idle` so the next Start begins a
   * brand new session (no leftover state).
   */
  discardSession: () => Promise<void>;
  /**
   * Stop and immediately start again. Finalizes the current session
   * normally (so it's saved and visible in the sessions list) and
   * begins a new session with a fresh UUID. Audio source + mic
   * selection + privacy mode are preserved across the swap.
   */
  restartSession: () => Promise<void>;
  /** ISO timestamp when the current session began (or null when idle). */
  sessionStartedAt: string | null;
  /**
   * Result of the cross-surface collision check, run as part of start().
   * When non-null, the start() call refused to begin because the
   * Record button surface is already active. Callers (AlwaysOnControl)
   * surface a confirm modal asking the user to stop the other one first.
   */
  startBlockedByRecord: boolean;
  /**
   * Reset the blocked state. Called by the UI after the user dismisses
   * the collision modal (whether they decide to abort or proceed).
   */
  clearStartBlocked: () => void;
  /**
   * Result of the one-shot device-capability probe (see
   * `utils/deviceDetection.ts`). Stable across the lifetime of the
   * provider — UI components read it to decide whether to render the
   * live-transcript/summary panes or the capture-only banner.
   */
  deviceCapability: DeviceCapability;
  /**
   * True when this device is a phone or tablet. In capture-only mode
   * we deliberately skip browser-side Parakeet 0.6B + Qwen 3 0.6B /
   * Gemma 4 E2B loads, MediaRecorder still captures audio and uploads
   * chunks, and the server reprocess pass produces the full transcript
   * + summary at meeting completion.
   */
  isCaptureOnlyMode: boolean;
  /**
   * Phase A.5 orphan sessions — recordings that started on this device
   * but never finalized (browser crash, hard refresh, tab close mid-
   * meeting). UI renders a banner offering Resume Upload / Discard.
   * Empty on mobile + desktop-fallback (local persistence is off).
   */
  orphanSessions: LocalAudioSessionMeta[];
  /** Session id currently being resumed via the orphan banner, or null. */
  resumingOrphan: string | null;
  /** Resume an orphan — verify against the server, backfill or full-
   *  audio upload as needed, kick the reprocess pipeline. */
  resumeOrphanSession: (sessionId: string) => Promise<void>;
  /** Drop the local chunks for an orphan without uploading. */
  discardOrphanSession: (sessionId: string) => Promise<void>;
  /**
   * v3.22.5 — True when the context claims an active recording but the
   * underlying capture pipeline appears dead. AlwaysOnControl uses this
   * to surface the "Reset recording state" escape hatch. Specifically:
   * state is in ``recording`` / ``paused`` / ``starting`` / ``stopping``
   * AND it's been more than ~60s since any audio chunk produced new
   * activity. Returns ``false`` while idle, immediately after start
   * (during the warm-up window), and during a healthy session where
   * chunks are flowing.
   */
  isStuck: boolean;
  /**
   * v3.22.5 — Manual escape hatch for stuck recording state. AlwaysOnControl
   * surfaces a "Reset recording state" button when ``isStuck`` is true.
   * On click this wipes the local claim, marks the server-side session
   * failed (best-effort POST /stop), and returns the context to idle.
   * Safe to call when nothing is live — every step is idempotent.
   */
  resetStuckState: (sessionIdHint?: string | null) => Promise<void>;
}

const AlwaysOnContext = createContext<AlwaysOnContextValue | null>(null);

function paragraphsFromText(text: string): SummaryParagraph[] {
  const timestamp = new Date().toISOString();
  return text
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => ({ text: part, timestamp }));
}

function formatSourceLabel(): string {
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  return nav.userAgentData?.platform || navigator.platform || 'Browser PWA';
}

function transcriptSegmentId(sessionId: string, segment: any): string {
  const text = String(segment.text || '').trim();
  const start = Number(segment.start ?? segment.start_time ?? 0);
  const end = Number(segment.end ?? segment.end_time ?? 0);
  const safeStart = Number.isFinite(start) ? start : 0;
  const safeEnd = Number.isFinite(end) ? end : 0;
  return [
    sessionId,
    safeStart.toFixed(2),
    safeEnd.toFixed(2),
    text.slice(0, 80),
  ].join(':');
}

/**
 * Build the X-MeetingOps-Org header for a given org slug. The recording
 * lifecycle calls (start-always-on, /full-audio, /finalize, /finalize-audio)
 * MUST all target the SAME org the session was created under, or the backend
 * lookup 404s with "Session not found." We capture that slug at create time
 * (sessionOrgSlugRef) rather than reading the live active org, so a
 * mid-session org switch can't repoint the upload at the wrong org.
 */
function orgHeader(slug: string | null | undefined): Record<string, string> {
  return slug ? { 'X-MeetingOps-Org': slug } : {};
}

export function AlwaysOnProvider({ children }: { children: ReactNode }) {
  const { activeOrganization } = useOrg();
  const { hasFeature } = useTierFeatures();
  const canUseServerProcessing = hasFeature('canonical_reprocess');
  const canUseServerProcessingRef = useRef(canUseServerProcessing);
  // Slug of the org the *current* server session was created under. Set in
  // createSession() and reused for /full-audio + /finalize at stop so all
  // three calls resolve to the same org server-side (fixes full-audio 404).
  const sessionOrgSlugRef = useRef<string | null>(null);
  const [state, setState] = useState<AlwaysOnState>('idle');
  const [vadActive, setVadActive] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  // ISO timestamp the user-visible session "Started" header reads.
  // Cleared in stop()/discard so the UI knows when to hide the header.
  const [sessionStartedAt, setSessionStartedAt] = useState<string | null>(null);
  const [chunksUploaded, setChunksUploaded] = useState(0);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [summary, setSummary] = useState<AlwaysOnSummary>(EMPTY_SUMMARY);
  const [liveTranscript, setLiveTranscript] = useState<LiveTranscriptSegment[]>([]);
  const [autoConfig, setAutoConfig] = useState<LiveSummaryAutoConfig>(() => getLiveSummaryAutoConfig());
  const [error, setError] = useState<string | null>(null);
  // Transient success notice surfaced after a clean Stop & save so the
  // user gets positive confirmation ("Saved — processing your meeting.")
  // instead of an empty surface that an orphan banner used to fill. Set
  // only when stop() finalizes without throwing; cleared on the next
  // start()/restart() and when the user dismisses it.
  const [saveConfirmation, setSaveConfirmation] = useState<string | null>(null);
  const [paidFeaturePrompt, setPaidFeaturePrompt] = useState<string | null>(null);
  const [statusText, setStatusText] = useState('Idle');
  const [sessionsCompleted, setSessionsCompleted] = useState(0);
  const [summarizing, setSummarizing] = useState(false);
  const [modelLoadProgress, setModelLoadProgress] = useState<number | null>(null);
  // Browser STT lifecycle. `sttBrowserFailed` is sticky for the *session*:
  // once a transcribe() throws we route subsequent chunks to the server
  // path until the user explicitly opts back in. This avoids the failure
  // mode of "every chunk hits the broken model again and the user sees
  // 30 toasts in a row."
  const [sttMode, setSttMode] = useState<SttMode>(() => getSTTMode());
  const [sttReady, setSttReady] = useState(false);
  const [sttLoadProgress, setSttLoadProgress] = useState<number | null>(null);
  const [sttLoadLabel, setSttLoadLabel] = useState<string | null>(null);
  const [sttBrowserFailed, setSttBrowserFailed] = useState(false);

  const sttModeRef = useRef<SttMode>(sttMode);
  const sttReadyRef = useRef(false);
  const sttBrowserFailedRef = useRef(false);
  const sttLoadAbortRef = useRef<AbortController | null>(null);

  // Phase A mobile gate. detectDevice() is a one-shot synchronous probe
  // — no async adapter requests, no permission prompts — so a useMemo
  // is safe. On phones / tablets we hard-skip Parakeet 0.6B + the
  // browser LLM loads downstream and the UI swaps to a capture-only
  // banner. Audio capture (fullAudioRecorder chunked upload) and the
  // server-side slice polling stay running, so the server reprocess
  // pass still produces a full transcript + summary at completion.
  const deviceCapability = useMemo(() => detectDevice(), []);
  const isCaptureOnlyMode = shouldShowCaptureOnlyBanner(deviceCapability);
  const allowBrowserInference = shouldRunBrowserInference(deviceCapability);

  useEffect(() => {
    canUseServerProcessingRef.current = canUseServerProcessing;
  }, [canUseServerProcessing]);

  // Tell the fetch interceptor whether we're actively capturing — keeps a
  // stale oauth2-proxy session refresh from yanking the page to /oauth2/start
  // mid-meeting. Cleared on unmount so a remount doesn't leave a dead claim.
  useEffect(() => {
    const live = state === 'starting' || state === 'recording' || state === 'paused' || state === 'stopping';
    setRecordingActive('alwaysOn', live);
    return () => setRecordingActive('alwaysOn', false);
  }, [state]);

  const showPaidFeaturePrompt = useCallback((message?: string) => {
    setPaidFeaturePrompt(
      message
        || 'You are on the free on-device tier. This recording stays private and local in your browser; upgrade to Pro to use server processing.',
    );
  }, []);

  const dismissPaidFeaturePrompt = useCallback(() => {
    setPaidFeaturePrompt(null);
  }, []);

  const dismissSaveConfirmation = useCallback(() => {
    setSaveConfirmation(null);
  }, []);

  const getServerProcessingError = useCallback(() => (
    'Server processing is available on Pro and Enterprise. Free recordings stay private and local in your browser.'
  ), []);

  // Phase A.5 orphan tracking. On desktop-capable browsers we open the
  // local IndexedDB store at mount and check for sessions that started
  // but never finalized (browser crash, tab close mid-meeting, hard
  // refresh). The UI surfaces a banner that lets the user Resume Upload
  // or Discard. List is refreshed after every stop() / discard() so
  // it stays accurate without polling.
  const [orphanSessions, setOrphanSessions] = useState<LocalAudioSessionMeta[]>([]);
  const [resumingOrphan, setResumingOrphan] = useState<string | null>(null);

  // Privacy mode: toggle persisted in localStorage. Locked-in per-session
  // at start() time so a mid-meeting setting flip doesn't half-upload.
  // When WebGPU is unavailable, the setting flips to "available off"
  // and we silently treat the session as standard.
  const [localOnlyPref, setLocalOnlyPref] = useState<boolean>(() => getLocalOnly());
  const localOnlyAvailable = useMemo(() => isPrivacyModeAvailable(), []);
  const [activeLocalOnly, setActiveLocalOnly] = useState<boolean>(false);
  const activeLocalOnlyRef = useRef<boolean>(false);
  useEffect(() => {
    activeLocalOnlyRef.current = activeLocalOnly;
  }, [activeLocalOnly]);
  const localOnlyForcedOff = (localOnlyPref || !canUseServerProcessing) && !localOnlyAvailable;

  // Device-aware capture state. activeMicDeviceId is the deviceId we're
  // RIGHT NOW capturing from (may differ from the saved preference if a
  // fallback fired). activeMicStream is the live MediaStream so the UI
  // can attach a VU meter without re-prompting permission.
  const [activeMicDeviceId, setActiveMicDeviceId] = useState<string | null>(
    () => getPreferredDeviceId(),
  );
  const [activeMicStream, setActiveMicStream] = useState<MediaStream | null>(
    null,
  );
  const [switchingDevice, setSwitchingDevice] = useState<boolean>(false);

  // Audio source mode (mic | tab | mic+tab). Persisted via the helper.
  // We mirror into state so the picker UI stays reactive, and listen for
  // cross-tab + custom events so a Settings flip in another tab updates
  // the picker. Locked at start() time so a mid-session change doesn't
  // half-swap the engine.
  const [audioSourceMode, setAudioSourceModeState] = useState<AudioSourceMode>(
    () => getAudioSourceMode(),
  );
  const activeAudioSourceModeRef = useRef<AudioSourceMode>('mic');
  // Tracks the opened source so we can register an "unexpected end"
  // listener on the underlying tab stream — when the user closes the
  // shared tab, the track ends and we want to gracefully stop the
  // session + toast.
  const openedSourceRef = useRef<OpenedAudioSource | null>(null);

  // Cross-surface guard. When the user clicks Start while the explicit
  // Record button is active, we set this flag so the UI can surface a
  // confirm modal instead of silently running two captures.
  const [startBlockedByRecord, setStartBlockedByRecord] = useState<boolean>(false);

  // Forward-ref to stop() — populated below after stop is defined.
  // Used inside the engine opener's track-ended handler, which is built
  // in ensureEngine() (defined before stop). Without this we'd have a
  // temporal-dead-zone reference.
  const stopRef = useRef<() => Promise<void>>(async () => undefined);
  // Forward-ref to the hot-swap recorder re-attach. Called from the engine
  // opener's onStreamChanged when a NEW MediaStream is bound mid-session
  // (device / source swap). Defined below where it can close over the
  // recorder factory + config. Held in a ref so ensureEngine doesn't churn.
  const reattachRecorderRef = useRef<(stream: MediaStream) => void>(() => undefined);
  // Track the USB-swap toast so we don't pile up duplicates when the user
  // wiggles a cable. Keyed by deviceId.
  const usbToastShownRef = useRef<Set<string>>(new Set());

  const engineRef = useRef<AlwaysOnVadEngine | null>(null);
  const stateRef = useRef<AlwaysOnState>('idle');
  const sessionRef = useRef<string | null>(null);
  // Stable idempotency key for the CURRENT start() lifecycle. Generated
  // once per start() (see start()) and sent on the /start-always-on create
  // so a retried createSession() — double-click, network retry, a
  // re-render that fires start() twice — reuses the SAME server session
  // instead of inserting a duplicate session row. The backend dedups on
  // this key within a 60s/active-status window and echoes
  // `idempotent_reuse:true` when it returns the existing session. Cleared
  // (regenerated) on every start(); null while idle.
  const clientSessionKeyRef = useRef<string | null>(null);
  const startedAtRef = useRef<number | null>(null);
  // v3.22.5 — stuck-state detection. We bump this whenever an audio
  // chunk lands (any path — server-text, server-audio, local-only) by
  // mirroring chunksUploaded into a ref via the effect below. The
  // health check derives ``isStuck`` from now() - lastChunkAtRef vs a
  // 60s threshold (longer than the engine's 30s silence timeout, so
  // truly-silent rooms don't trip it; shorter than the 90s "phone tab
  // probably got swapped" window). null = no chunk has ever landed in
  // the current session (warm-up window).
  const lastChunkAtRef = useRef<number | null>(null);
  const [isStuck, setIsStuck] = useState<boolean>(false);
  // Latest live transcript text, readable inside the stop-time closure
  // without dep churn. Used by the no-slices summary fallback so short
  // local-only meetings (under the first slice threshold) still end with
  // a real summary instead of nothing (Ceejay, 2026-07-16 user testing).
  const transcriptTextRef = useRef<string>('');
  const uploadQueueRef = useRef<Promise<void>>(Promise.resolve());
  const splitInFlightRef = useRef(false);
  const summarizeAbortRef = useRef<AbortController | null>(null);
  const transcriptSocketRef = useRef<WebSocket | null>(null);
  const autoDebounceRef = useRef<number | null>(null);
  const lastAutoFireRef = useRef<number>(Date.now());
  const summarizingRef = useRef(false);
  // Parallel full-session MediaRecorder. Lives alongside the VAD engine so
  // the server can re-run STT + diarization + speaker id + a quality final
  // summary against the contiguous audio after the meeting ends. v3.26.9
  // LOCAL-then-UPLOAD: the recorder BUFFERS the whole meeting (IndexedDB
  // on desktop, in-memory on mobile) and uploads it ONCE at Stop via
  // /full-audio. It runs in privacy mode too (localOnly:true) but never
  // hits the network there.
  const fullAudioRecorderRef = useRef<FullAudioRecorderHandle | null>(null);
  // True when the active recorder buffers IN MEMORY (mobile / no-IDB)
  // rather than IndexedDB. Drives which getAssembledBlob() stop() reads:
  // the recorder handle's own (in-memory) vs localAudioStore's (IDB).
  const recorderInMemoryRef = useRef<boolean>(false);
  // Slice polling — replaces the in-browser Qwen 3 0.6B slice rolling for
  // standard-mode (server) sessions. Privacy mode keeps the legacy local
  // slice path (see triggerSummarize below).
  const sliceFetchAbortRef = useRef<AbortController | null>(null);
  // PostHog `summary_slice_generated` dedup. The server polling loop
  // re-GETs the full slice stack every 5s, so we track which slice ids
  // have already been reported and only fire for genuinely-new ones.
  // Reset on each start(). Shared with the browser/local slice path
  // (they never run for the same session).
  const trackedSliceIdsRef = useRef<Set<string>>(new Set());
  // Mirror of transcriptWordCount so stop() (a memoized callback defined
  // before transcriptWordCount) reads the current count, not a stale one.
  const transcriptWordCountRef = useRef<number>(0);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<LiveSummaryAutoConfig>).detail;
      if (detail) setAutoConfig(detail);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === 'meetingops.liveSummary.autoConfig.v1') {
        setAutoConfig(getLiveSummaryAutoConfig());
      }
    };
    window.addEventListener('meetingops:live-summary-auto-config', handler as EventListener);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('meetingops:live-summary-auto-config', handler as EventListener);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  useEffect(() => {
    sessionRef.current = currentSessionId;
  }, [currentSessionId]);

  // Keep refs in sync — VAD callbacks need to read the latest values
  // without re-binding (the callbacks are created via createVadEngine
  // once per provider mount).
  useEffect(() => {
    sttModeRef.current = sttMode;
  }, [sttMode]);
  useEffect(() => {
    sttReadyRef.current = sttReady;
  }, [sttReady]);
  useEffect(() => {
    sttBrowserFailedRef.current = sttBrowserFailed;
  }, [sttBrowserFailed]);

  // Settings UI writes via setSTTMode() + dispatches an event. Pick it up
  // in any open tab so the picker stays in sync.
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<SttMode>).detail;
      if (detail) setSttMode(detail);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === 'meetingops.stt.mode.v1') {
        setSttMode(getSTTMode());
      }
    };
    window.addEventListener('meetingops:stt-mode', handler as EventListener);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('meetingops:stt-mode', handler as EventListener);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<boolean>).detail;
      if (typeof detail === 'boolean') setLocalOnlyPref(detail);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === 'meetingops.privacyMode.localOnly') {
        setLocalOnlyPref(getLocalOnly());
      }
    };
    window.addEventListener('meetingops:privacy-mode', handler as EventListener);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('meetingops:privacy-mode', handler as EventListener);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  // Audio source mode listener — keeps the picker in sync if Settings or
  // another tab flips it. Does NOT swap the engine mid-session; the new
  // mode applies on the next start().
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<AudioSourceMode>).detail;
      if (detail === 'mic' || detail === 'tab' || detail === 'mic+tab') {
        setAudioSourceModeState(detail);
      }
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === AUDIO_SOURCE_MODE_KEY) {
        setAudioSourceModeState(getAudioSourceMode());
      }
    };
    window.addEventListener(AUDIO_SOURCE_MODE_EVENT, handler as EventListener);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(AUDIO_SOURCE_MODE_EVENT, handler as EventListener);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const updateAudioSourceMode = useCallback((mode: AudioSourceMode) => {
    // Persist immediately. The local state will follow via the event
    // listener above, but we also call setState here so a same-tick
    // re-render sees the new value without round-tripping through the
    // event loop.
    setAudioSourceMode(mode);
    setAudioSourceModeState(mode);
  }, []);

  const clearStartBlocked = useCallback(() => {
    setStartBlockedByRecord(false);
  }, []);

  // Kick off the browser STT load whenever the mode flips to
  // 'browser-parakeet' (including first mount). Don't block the UI on it
  // — the VAD `onSpeechEnd` handler routes through the server path until
  // the model is ready, then auto-switches to browser. This is the same
  // idea as inBrowserLLM's lazy load.
  useEffect(() => {
    // Phase A mobile gate. On phones / tablets we never attempt to
    // load Parakeet 0.6B INT8 — the WASM fallback is sub-realtime on
    // iOS Safari and the model weights blow past the mobile origin
    // storage cap. Audio still captures + uploads via the chunked
    // MediaRecorder path, and the server reprocess pass at completion
    // produces the full transcript. The label tells anyone debugging
    // why STT isn't initializing.
    if (!allowBrowserInference) {
      setSttReady(false);
      setSttLoadProgress(null);
      setSttLoadLabel('Skipped on mobile');
      sttLoadAbortRef.current?.abort();
      sttLoadAbortRef.current = null;
      return;
    }
    if (sttMode !== 'browser-parakeet') {
      // We can't actually unload the model from another tab; this just
      // tells our local context to stop trying.
      setSttReady(false);
      setSttLoadProgress(null);
      setSttLoadLabel(null);
      sttLoadAbortRef.current?.abort();
      sttLoadAbortRef.current = null;
      return;
    }
    if (inBrowserSTT.isReady()) {
      setSttReady(true);
      setSttLoadProgress(1);
      setSttLoadLabel('Ready');
      return;
    }

    const controller = new AbortController();
    sttLoadAbortRef.current = controller;
    setSttLoadProgress(0);
    setSttLoadLabel('Loading');
    setSttBrowserFailed(false);

    inBrowserSTT
      .load({
        signal: controller.signal,
        onProgress: (pct, label) => {
          setSttLoadProgress(pct);
          if (label) setSttLoadLabel(label);
        },
      })
      .then(() => {
        if (controller.signal.aborted) return;
        setSttReady(true);
        setSttLoadProgress(1);
        setSttLoadLabel('Ready');
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : String(err);
        // We failed to even load. Mark failed and fall back to server.
        // The picker will show this state and the user can retry.
        setSttBrowserFailed(true);
        setSttReady(false);
        setSttLoadProgress(null);
        setSttLoadLabel(`Load failed: ${message.slice(0, 80)}`);
        // eslint-disable-next-line no-console
        console.warn('[always-on] Browser STT load failed:', err);
      });

    return () => {
      controller.abort();
      if (sttLoadAbortRef.current === controller) {
        sttLoadAbortRef.current = null;
      }
    };
  }, [sttMode, allowBrowserInference]);

  useEffect(() => {
    if (state !== 'recording' && state !== 'paused') return;
    const tick = window.setInterval(() => {
      if (!startedAtRef.current) return;
      setElapsedSeconds(Math.floor((Date.now() - startedAtRef.current) / 1000));
      // Heartbeat the cross-surface presence flag while a session is live,
      // so a crashed/killed tab leaves a STALE flag (readers self-heal)
      // instead of a sticky one that blocks every future Start.
      if (stateRef.current === 'recording' || stateRef.current === 'paused') {
        beatAlwaysOnActive();
      }
    }, 1000);
    return () => window.clearInterval(tick);
  }, [state]);

  const appendTranscriptSegments = useCallback((segments: any[], sessionId: string) => {
    if (!segments.length) return;
    const timestamp = new Date().toISOString();
    const normalized = segments
      .map((segment): LiveTranscriptSegment | null => {
        const text = String(segment.text || '').trim();
        if (!text) return null;
        const start = Number(segment.start ?? segment.start_time ?? 0);
        const end = Number(segment.end ?? segment.end_time ?? 0);
        return {
          id: transcriptSegmentId(sessionId, segment),
          sessionId,
          text,
          speaker: segment.speaker || segment.speaker_id || null,
          start: Number.isFinite(start) ? start : 0,
          end: Number.isFinite(end) ? end : 0,
          timestamp,
        };
      })
      .filter((segment): segment is LiveTranscriptSegment => segment !== null);

    if (!normalized.length) return;
    setLiveTranscript((prev) => {
      const seen = new Set(prev.map((segment) => segment.id));
      const next = [...prev];
      for (const segment of normalized) {
        if (seen.has(segment.id)) continue;
        seen.add(segment.id);
        next.push(segment);
      }
      return next.slice(-80);
    });
  }, []);

  // v3.26.9 LOCAL-then-UPLOAD: the live transcript is now driven entirely
  // by in-browser STT (see uploadChunk → appendTranscriptSegments). We no
  // longer open the server transcription websocket during recording —
  // nothing streams to the server mid-meeting, so the socket would never
  // receive a `transcription` broadcast. The server's canonical
  // transcript is produced by the /full-audio reprocess at Stop and is
  // viewed on the session detail page. `transcriptSocketRef` stays
  // declared (null) so the unmount cleanup's `?.close()` is a safe no-op.

  const createSession = useCallback(async (): Promise<string> => {
    // Local-only mode skips the server entirely. Session id lives only
    // in IndexedDB; the rest of the always-on plumbing routes through
    // the same id (with the `local-` prefix) but no network traffic
    // ever fires for chunks / slices / finalize.
    if (activeLocalOnlyRef.current) {
      const session = await createLocalSession();
      setCurrentSessionId(session.id);
      sessionRef.current = session.id;
      return session.id;
    }

    if (!canUseServerProcessingRef.current) {
      showPaidFeaturePrompt();
      throw new Error(getServerProcessingError());
    }

    if (!activeOrganization) {
      throw new Error('No active organization is selected.');
    }

    const response = await fetch(`${config.apiUrl}/api/recordings/start-always-on`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...orgHeader(activeOrganization.slug) },
      credentials: 'include',
      body: JSON.stringify({
        org_id: activeOrganization.id,
        source_label: formatSourceLabel(),
        // Idempotency guard — a retried create (double-click / network
        // retry) reuses the existing server session instead of inserting
        // a duplicate row. Omitted (undefined) the first time the ref
        // hasn't been seeded so behavior is exactly as before.
        client_session_key: clientSessionKeyRef.current ?? undefined,
      }),
    });
    if (!response.ok) {
      const detail = await response.text();
      if (response.status === 403) {
        showPaidFeaturePrompt();
        throw new Error(getServerProcessingError());
      }
      throw new Error(detail || 'Failed to start always-on session.');
    }
    const session = await response.json();
    if (session.idempotent_reuse) {
      // The server returned an EXISTING session for our key rather than a
      // new row — a retried create was deduped. Purely informational.
      // eslint-disable-next-line no-console
      console.info('[always-on] reused existing server session (idempotent create)');
    }
    const sessionId = String(session.id);
    setCurrentSessionId(sessionId);
    sessionRef.current = sessionId;
    // Pin the org this session lives under for stop-time /full-audio +
    // /finalize, regardless of any later active-org switch.
    sessionOrgSlugRef.current = activeOrganization.slug ?? null;
    return sessionId;
  }, [activeOrganization]);

  const finalizeSession = useCallback(async (sessionId: string): Promise<void> => {
    // Local-only finalize is handled inline by stop() (final-summary
    // roll-up + endLocalSession). Don't touch the network here.
    if (activeLocalOnlyRef.current) return;
    if (!canUseServerProcessingRef.current) {
      showPaidFeaturePrompt();
      throw new Error(getServerProcessingError());
    }
    const response = await fetch(`${config.apiUrl}/api/recordings/sessions/${sessionId}/finalize`, {
      method: 'POST',
      headers: orgHeader(sessionOrgSlugRef.current),
      credentials: 'include',
    });
    if (!response.ok) {
      const detail = await response.text();
      if (response.status === 403) {
        showPaidFeaturePrompt();
        throw new Error(getServerProcessingError());
      }
      throw new Error(detail || 'Failed to finalize always-on session.');
    }
    // Kick off the server-side reprocess pipeline if we uploaded full
    // audio chunks during the session. The endpoint is idempotent +
    // skips itself when no chunks landed (privacy mode would never reach
    // this branch anyway because activeLocalOnlyRef.current short-circuits
    // above). We fire-and-forget — the user can navigate to the session
    // detail page to watch the reprocess banner; we don't want stop() to
    // block on a server-side STT pipeline that takes 30+ seconds.
    try {
      await fetch(
        `${config.apiUrl}/api/recordings/sessions/${sessionId}/finalize-audio`,
        {
          method: 'POST',
          headers: orgHeader(sessionOrgSlugRef.current),
          credentials: 'include',
        },
      ).then(async (response) => {
        if (response.status === 403) {
          showPaidFeaturePrompt();
        }
      });
    } catch (err) {
      // Non-fatal — the live transcript stays as the canonical record.
      // eslint-disable-next-line no-console
      console.warn('[always-on] finalize-audio kick-off failed:', err);
    }
  }, []);

  /**
   * Mark a just-finalized session's local IDB row as finalized, then wipe
   * its chunks. Crucial for the orphan-banner false-positive: a successful
   * server finalize doesn't otherwise touch the local row, so the
   * state==='idle' orphan rescan would find `finalized:false && chunk_count>0`
   * and surface "Found unfinished recording" right after a clean Stop.
   *
   * Setting `finalized:true` is awaited (synchronous w.r.t. stop()) so the
   * getOrphanedSessions() filter (`!m.finalized`) already excludes the row
   * even before the async wipe lands — i.e. the rescan can never flag it.
   * The wipe is fire-and-forget so stop() still returns fast.
   *
   * Best-effort: any IDB error is swallowed (we already finalized
   * server-side; a lingering local row at worst shows a stale banner the
   * user can Discard, never data loss). No-op when local persistence is
   * unavailable (mobile / desktop-fallback), where there's no IDB row.
   */
  const markLocalSessionFinalizedSafe = useCallback(async (sessionId: string) => {
    if (!supportsLocalPersistence(deviceCapability)) return;
    if (!localAudioStore.isAvailable()) return;
    try {
      const meta = await localAudioStore.getSessionMetadata(sessionId);
      if (!meta || meta.finalized) return;
      // Reuse the recorded client hash if we have one; the server has
      // already accepted finalize so the value is informational here.
      await localAudioStore.setSessionFinalized(sessionId, meta.client_sha256 ?? '');
      void localAudioStore.wipeSession(sessionId).catch((err) => {
        // eslint-disable-next-line no-console
        console.warn('[always-on] local wipe after finalize failed (non-fatal):', err);
      });
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[always-on] mark local finalized failed (non-fatal):', err);
    }
  }, [deviceCapability]);

  // v3.26.9 LOCAL-then-UPLOAD: the during-recording server-streaming
  // helpers (uploadAudioChunk → /chunks, uploadTextChunk → /chunks-text)
  // were REMOVED. Nothing is sent to the server while recording; the live
  // transcript is browser-STT-only and the whole audio file is uploaded
  // once at Stop via /full-audio. The backend /chunks + /chunks-text
  // endpoints are left in place for one release for backward-compat but
  // are no longer called from here.

  // v3.26.9 LOCAL-then-UPLOAD: during a recording we send NOTHING to the
  // server. Every VAD utterance is transcribed IN-BROWSER and appended to
  // the on-screen live transcript only. The whole audio file is uploaded
  // once at Stop (stop() → postFullAudio → /full-audio), and the server's
  // canonical reprocess overwrites whatever live text existed. There is
  // no /chunks-text or /chunks (audio) POST mid-meeting anymore, so a
  // clean Stop can never leave unprocessed server chunks.
  //
  // Browsers without WebGPU/browser-STT simply get no live captions on a
  // paid account — the full transcript still arrives after Stop from the
  // server reprocess. We surface a one-time status note in that case.
  const liveOnlyStTUnavailableNotedRef = useRef(false);
  const uploadChunk = useCallback(async (chunk: VadAudioChunk): Promise<void> => {
    let sessionId = sessionRef.current;
    if (!sessionId) {
      sessionId = await createSession();
    }

    const localOnly = activeLocalOnlyRef.current;

    // Browser STT is the ONLY live-transcript source. It drives captions
    // in BOTH local-only (privacy) and standard server-backed sessions.
    // The only difference: local-only ALSO appends the text to the
    // on-device local session store (localSessionStore) so the privacy
    // pipeline has a transcript; standard sessions don't, because the
    // server reprocess produces the canonical transcript at Stop.
    const browserSttUsable =
      sttModeRef.current === 'browser-parakeet'
      && sttReadyRef.current
      && !sttBrowserFailedRef.current;

    if (!browserSttUsable) {
      // No in-browser STT available for live captions.
      if (localOnly) {
        // Local-only REQUIRES browser STT — there is no server fallback
        // and nothing leaves the device. Surface a clear error.
        setError(
          'Local STT is unavailable in this browser. Live captions and the on-device transcript need a WebGPU-capable browser such as current Chrome or Edge.',
        );
      } else if (!liveOnlyStTUnavailableNotedRef.current) {
        // Standard session: live captions are unavailable but the full
        // transcript still arrives after Stop from the server reprocess.
        liveOnlyStTUnavailableNotedRef.current = true;
        setStatusText('Live transcript unavailable — full transcript will be ready after Stop');
      }
      setChunksUploaded((count) => count + 1);
      return;
    }

    try {
      const text = await inBrowserSTT.transcribe(chunk.blob);
      const trimmed = (text || '').trim();
      if (!trimmed) {
        // Browser model returned empty (likely a misfire or noise).
        setChunksUploaded((count) => count + 1);
        return;
      }
      const elapsed = Math.max(0, chunk.elapsedSeconds);
      // Local-only sessions persist the live text to the on-device
      // session store so the privacy STT/LLM pipeline has a transcript.
      // Standard sessions skip this — the server reprocess at Stop owns
      // the canonical transcript.
      if (localOnly) {
        const localChunk: LocalTranscriptChunk = {
          text: trimmed,
          elapsedSeconds: elapsed,
          durationSeconds: chunk.durationSeconds || 0,
          provenance: CLIENT_STT_PROVENANCE,
          createdAt: new Date().toISOString(),
        };
        await appendLocalChunk(sessionId, localChunk);
      }
      // On-screen live transcript — the single live-view surface. No
      // websocket, no server round-trip.
      appendTranscriptSegments(
        [{
          text: trimmed,
          start: elapsed,
          end: elapsed + (chunk.durationSeconds || 0),
          speaker: null,
        }],
        sessionId,
      );
      setChunksUploaded((count) => count + 1);
      return;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // eslint-disable-next-line no-console
      console.warn('[always-on] Live browser STT failed for chunk:', message);
      if (localOnly) {
        // Nothing leaves the device; the chunk's live text is lost but
        // the buffered audio is intact for the Stop-time local pass.
        setError(`Local STT failed: ${message.slice(0, 120)}. Live caption dropped (nothing leaves the device).`);
      } else {
        // Standard session: the live caption is dropped, but the audio is
        // buffered locally and the server reprocess at Stop will produce
        // the full transcript regardless.
        setSttBrowserFailed(true);
        setStatusText('Live transcript paused — full transcript will be ready after Stop');
      }
      setChunksUploaded((count) => count + 1);
      return;
    }
  }, [appendTranscriptSegments, createSession]);

  const queueChunkUpload = useCallback((chunk: VadAudioChunk) => {
    uploadQueueRef.current = uploadQueueRef.current
      .then(() => uploadChunk(chunk))
      .catch((err) => {
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
        setStatusText('Upload failed');
      });
  }, [uploadChunk]);

  // DORMANT as of the always-on fragmentation fix: this helper is no
  // longer wired to the VAD engine's `onSilenceThreshold` (see
  // ensureEngine), so it does NOT run automatically during a meeting.
  // It finalizes the current session and opens a fresh one, and is kept
  // intact as the building block for the planned post-process "Split
  // into meetings" feature. The user-facing "Start new session" button
  // does NOT call this — it goes through restartSession() (stop+start).
  const splitSession = useCallback(async () => {
    if (splitInFlightRef.current || stateRef.current !== 'recording') return;
    const sessionId = sessionRef.current;
    if (!sessionId) return;

    splitInFlightRef.current = true;
    setVadActive(false);
    setStatusText('Listening for next meeting...');
    try {
      await uploadQueueRef.current;
      setSessionsCompleted((count) => count + 1);
      if (activeLocalOnlyRef.current) {
        // Local split: close the IndexedDB session without a final
        // summary roll-up — we never want to block the next session on
        // an LLM pass. The user can re-open the local session later if
        // they want a roll-up retroactively (out of scope for v1).
        endLocalSession(sessionId, null).catch((err) => {
          // eslint-disable-next-line no-console
          console.warn('[always-on] Local split end failed:', err);
        });
      } else {
        void finalizeSession(sessionId).catch((err) => {
          const message = err instanceof Error ? err.message : String(err);
          setError(message);
        });
      }
      if (stateRef.current === 'recording') {
        await createSession();
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    } finally {
      splitInFlightRef.current = false;
    }
  }, [createSession, finalizeSession]);

  const getElapsed = useCallback(() => {
    if (!startedAtRef.current) return 0;
    return (Date.now() - startedAtRef.current) / 1000;
  }, []);

  const ensureEngine = useCallback(async () => {
    if (!engineRef.current) {
      const { createVadEngine } = await loadVadEngineModule();
      engineRef.current = createVadEngine({
        getElapsedSeconds: getElapsed,
        // In-recording silence-split is disabled ENTIRELY by not wiring an
        // `onSilenceThreshold` handler below — see the note there. The
        // right architecture is to NEVER split during capture: the audio
        // file stays whole, the user reviews the recording in
        // /sessions/{id} and applies splits as a post-process operation
        // with full transcript context. The post-process "Split into
        // meetings" feature is on the v3.24+ roadmap (see
        // docs/roadmap/post-process-split.md).
        //
        // NOTE (the v3.23.2 hotfix bug this replaces): the previous attempt
        // set `silenceThresholdMs: Number.MAX_SAFE_INTEGER` intending the
        // silence timer to "never" fire. But `window.setTimeout` truncates
        // its delay to a signed 32-bit int — MAX_SAFE_INTEGER (9007199254740991)
        // truncates to -1, which the HTML spec clamps to 0ms. So the timer
        // fired on the NEXT TICK after every `onSpeechEnd`, calling
        // splitSession after each utterance and fragmenting one meeting into
        // dozens of one-utterance sessions. We keep a sane finite value here
        // purely so the engine's internal timer math stays well-defined; the
        // real guarantee is that nothing consumes the callback.
        silenceThresholdMs: 30_000,
        // Read from localStorage at each start/swap so cross-tab Settings
        // edits propagate without a page reload.
        getPreferredDeviceId: () => getPreferredDeviceId(),
        // Source-aware stream opener. Reads the locked-in mode from the
        // ref each call so a session that was started in 'tab' stays in
        // 'tab' even if the user flips the picker mid-session.
        openStream: async (deviceId): Promise<VadStreamOpenResult> => {
          const mode = activeAudioSourceModeRef.current;
          // Dispose any previously-opened source (e.g. on hot-swap). The
          // engine also calls releaseExtras via stopCurrentStream(), but
          // this is belt-and-suspenders.
          const previous = openedSourceRef.current;
          openedSourceRef.current = null;
          if (previous) {
            try {
              previous.cleanup();
            } catch {
              // ignore
            }
          }

          const opened = await openAudioSourceStream(mode, {
            preferredDeviceId: deviceId,
          });
          openedSourceRef.current = opened;

          // Register the tab-closed listener. When the underlying tab
          // stream ends (user closes the captured tab, navigates away,
          // or clicks "Stop sharing" in the Chrome bar) we gracefully
          // stop the session and surface a toast.
          opened.onUnexpectedEnd((reason) => {
            // Only fire if we're still the active source — defensive
            // against a stale closure after stop()/restart.
            if (openedSourceRef.current !== opened) return;
            const label = reason === 'tab' ? 'Captured tab closed' : 'Audio source ended';
            showToast.warning(`${label} — recording stopped.`);
            // Defer the actual stop to the next tick so the engine
            // doesn't race with track-ended teardown inside MicVAD.
            window.setTimeout(() => {
              if (stateRef.current === 'recording' || stateRef.current === 'paused') {
                void stopRef.current();
              }
            }, 0);
          });

          return {
            stream: opened.stream,
            // Mic-only sources can return their deviceId; tab and mic+tab
            // return null since "deviceId" doesn't apply.
            deviceId: mode === 'mic' ? deviceId : null,
            fellBack: false,
            // The VAD engine calls releaseExtras as part of stopCurrentStream().
            // That hooks into our opener's cleanup, which stops raw streams +
            // closes the AudioContext used for mic+tab mixing.
            releaseExtras: () => {
              if (openedSourceRef.current === opened) {
                openedSourceRef.current = null;
              }
              opened.cleanup();
            },
          };
        },
        onDeviceChanged: (deviceId) => {
          setActiveMicDeviceId(deviceId);
        },
        onStreamChanged: (stream) => {
          setActiveMicStream(stream);
          // v3.26.9 — device/source hot-swap re-attach. The VAD engine
          // rebuilds on switchDevice and binds a NEW MediaStream; the
          // full-audio recorder was bound to the OLD stream and would go
          // silent after the swap. Re-attach it to the new stream so the
          // continuous buffer keeps filling. No-op on the initial start
          // bind (the recorder doesn't exist yet at that point).
          if (stream) reattachRecorderRef.current(stream);
        },
        onSpeechStart: () => {
          setVadActive(true);
          setStatusText('Speech detected');
        },
        onSpeechEnd: (chunk) => {
          setVadActive(false);
          setStatusText('Processing speech chunk');
          queueChunkUpload(chunk);
        },
        onMisfire: () => {
          setVadActive(false);
        },
        // DELIBERATELY NOT WIRED. Leaving `onSilenceThreshold` undefined is
        // what guarantees a meeting = exactly one server session: the engine
        // never auto-splits on silence, so the session created at start()
        // receives EVERY chunk (chunks-text + audio-chunks) and is finalized
        // exactly once, on the user's explicit Stop/Pause. The automatic
        // splitSession() helper remains defined for a future post-process
        // "Split into meetings" feature but is intentionally unreferenced
        // here. The MANUAL "Start new session" button is a separate path
        // (restartSession → stop()+start()) and is unaffected.
        onError: (message) => setError(message),
      });
    }
    return engineRef.current;
    // `stop` is intentionally omitted from deps — it's a stable callback
    // and including it would re-create the engine on every state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [getElapsed, queueChunkUpload]);

  /**
   * Hot-swap the active mic mid-session. Does NOT touch the persisted
   * preference — that's by design, so a per-session override doesn't
   * leak into the default. Pause-switch-resume is ~few-hundred-ms in
   * practice; we set `switchingDevice` while it's happening so the UI
   * can disable the picker.
   */
  const switchMicDevice = useCallback(
    async (deviceId: string | null) => {
      // If not in an active recording, this is a no-op. AlwaysOnControl
      // disables the picker in that state, but be defensive.
      if (stateRef.current !== 'recording' && stateRef.current !== 'paused') {
        return;
      }
      const engine = engineRef.current;
      if (!engine) return;
      setSwitchingDevice(true);
      const wasPaused = stateRef.current === 'paused';
      try {
        // Resume first if paused; switchDevice tears down + recreates the
        // VAD instance, and it's simpler to do that from a running state.
        if (wasPaused) {
          await engine.resume();
        }
        const opened = await engine.switchDevice(deviceId);
        // The engine fires onDeviceChanged already; this is just for the
        // optimistic update in case the callback hasn't landed yet.
        setActiveMicDeviceId(opened);
        if (wasPaused) {
          // Restore prior pause state — don't surprise the user.
          await engine.pause();
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setError(`Mic swap failed: ${message}`);
      } finally {
        setSwitchingDevice(false);
      }
    },
    [],
  );

  /**
   * Hot-swap the audio source mode mid-session. Updates the locked ref
   * the engine opener reads from, persists to localStorage, then calls
   * engine.switchDevice() to trigger the full opener path — which now
   * sees the new mode and reopens mic / tab / mic+tab accordingly.
   *
   * No special pause handling beyond what switchMicDevice already does;
   * the engine teardown is the same regardless of which mode we land in.
   * Gap is roughly the same as switchMicDevice (~few hundred ms).
   */
  const switchAudioSource = useCallback(
    async (mode: AudioSourceMode) => {
      // If not active, just persist for next start — same semantics as
      // setAudioSourceMode would have given.
      if (stateRef.current !== 'recording' && stateRef.current !== 'paused') {
        setAudioSourceMode(mode);
        setAudioSourceModeState(mode);
        activeAudioSourceModeRef.current = mode;
        return;
      }
      // Same mode — nothing to do.
      if (activeAudioSourceModeRef.current === mode) {
        return;
      }
      const engine = engineRef.current;
      if (!engine) return;
      setSwitchingDevice(true);
      const wasPaused = stateRef.current === 'paused';
      // Capture the previous mode so we can revert if the swap fails.
      const previousMode = activeAudioSourceModeRef.current;
      // Lock the new mode in BEFORE calling switchDevice — the engine
      // opener reads activeAudioSourceModeRef inside its callback, so
      // it has to be flipped first or the reopen happens with the old
      // mode. Also persist + flip state so the UI chip reflects the
      // new target immediately.
      activeAudioSourceModeRef.current = mode;
      setAudioSourceMode(mode);
      setAudioSourceModeState(mode);
      try {
        if (wasPaused) {
          await engine.resume();
        }
        // For mic-based modes we want to preserve the user's current
        // mic deviceId; for tab-only the deviceId is irrelevant and the
        // opener ignores it. Reading getActiveDeviceId() so a per-session
        // mic override survives the source switch.
        const currentDeviceId = engine.getActiveDeviceId();
        const opened = await engine.switchDevice(currentDeviceId);
        setActiveMicDeviceId(opened);
        if (wasPaused) {
          await engine.pause();
        }
        // Friendly use-case label for the toast — matches the new
        // /record page framing so the user sees the same words they
        // clicked, not the underlying mechanism.
        const friendlyLabel =
          mode === 'mic'
            ? 'In-person (mic only)'
            : mode === 'tab'
              ? 'Online meeting (tab only)'
              : 'Mixed (mic + tab)';
        showToast.success(`Audio source switched to ${friendlyLabel}`);
      } catch (err) {
        // Roll back the locked mode + state so the UI and engine stay
        // in sync with reality (we never opened the new source).
        activeAudioSourceModeRef.current = previousMode;
        setAudioSourceMode(previousMode);
        setAudioSourceModeState(previousMode);
        const message = err instanceof Error ? err.message : String(err);
        setError(`Source swap failed: ${message}`);
      } finally {
        setSwitchingDevice(false);
      }
    },
    [],
  );

  // devicechange listener — fires on USB plug/unplug. Three behaviors:
  //   1. New USB device appears: if auto-prefer-USB is ON and we're
  //      recording, surface a "Switch?" toast. If idle, just notify so
  //      the user knows their picker has fresh options.
  //   2. Currently-active device disappears (recording): auto-fall-back
  //      to system default and toast a notification.
  //   3. Otherwise: silent re-enumeration (Settings page will see it
  //      through its own useAudioDevices hook).
  useEffect(() => {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices) return;

    // Snapshot current input devices so we can diff. Initial fetch may be
    // label-less if no permission has been granted yet; that's fine — we
    // only care about presence/absence of deviceIds for diffing.
    let prevDevices: MediaDeviceInfo[] = [];
    const refreshDevices = async () => {
      try {
        const all = await navigator.mediaDevices.enumerateDevices();
        return all.filter((d) => d.kind === 'audioinput');
      } catch {
        return [];
      }
    };

    let cancelled = false;
    void (async () => {
      prevDevices = await refreshDevices();
      if (cancelled) return;
    })();

    const onDeviceChange = async () => {
      const fresh = await refreshDevices();
      const prev = prevDevices;
      prevDevices = fresh;

      const prevIds = new Set(prev.map((d) => d.deviceId));
      const freshIds = new Set(fresh.map((d) => d.deviceId));

      // (1) New USB device(s) appeared
      const newUsb = fresh.filter(
        (d) => !prevIds.has(d.deviceId) && isUsbAudioDevice(d),
      );
      // (2) Active device disappeared
      const activeId =
        engineRef.current?.getActiveDeviceId() ?? getPreferredDeviceId();
      const activeLost =
        activeId && prevIds.has(activeId) && !freshIds.has(activeId);

      if (
        activeLost &&
        (stateRef.current === 'recording' || stateRef.current === 'paused')
      ) {
        // Engine fall-back: switch to system default and toast.
        try {
          await engineRef.current?.switchDevice(null);
        } catch (err) {
          // engine will surface via onError; nothing else to do.
        }
        showToast.warning(
          'Selected microphone was disconnected. Switched to system default.',
        );
        // Clear the now-invalid saved preference so the next session
        // doesn't try to use it again. (The user can pick a new one in
        // Settings; we just stop pointing at a ghost.)
        if (getPreferredDeviceId() === activeId) {
          setPreferredDeviceId(null);
        }
      }

      if (
        newUsb.length > 0 &&
        getAutoPreferUsb() &&
        (stateRef.current === 'recording' || stateRef.current === 'paused')
      ) {
        // Only one toast per new device id per page-life. Wiggling a
        // cable shouldn't spam.
        for (const dev of newUsb) {
          if (usbToastShownRef.current.has(dev.deviceId)) continue;
          usbToastShownRef.current.add(dev.deviceId);
          const label = dev.label?.trim() || 'New USB microphone';
          const toastId = toastify.info(
            (
              <div className="flex items-center gap-3">
                <div className="flex-1 text-sm">
                  <div className="font-medium">USB mic detected</div>
                  <div className="text-xs opacity-80">{label}</div>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    void switchMicDevice(dev.deviceId);
                    // Update the saved default too — the user explicitly
                    // chose this mic, persist for future sessions.
                    setPreferredDeviceId(dev.deviceId);
                    toastify.dismiss(toastId);
                  }}
                  className="rounded bg-emerald-500/30 px-2 py-1 text-xs font-semibold text-emerald-100 hover:bg-emerald-500/50"
                >
                  Switch
                </button>
                <button
                  type="button"
                  onClick={() => toastify.dismiss(toastId)}
                  className="rounded bg-zinc-700/50 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-600/60"
                >
                  Keep current
                </button>
              </div>
            ),
            {
              autoClose: 15000,
              closeOnClick: false,
              draggable: false,
            },
          );
        }
      }
    };

    navigator.mediaDevices.addEventListener('devicechange', onDeviceChange);
    return () => {
      cancelled = true;
      navigator.mediaDevices.removeEventListener(
        'devicechange',
        onDeviceChange,
      );
    };
    // switchMicDevice is stable (refs); listing it as a dep would cause
    // a churn on every render. We intentionally leave it out.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const start = useCallback(async () => {
    if (stateRef.current !== 'idle') return;

    // Cross-surface guard. If the explicit Record button is in use,
    // refuse to start until the user resolves the collision. The UI
    // shows a confirm modal driven by `startBlockedByRecord`; the
    // caller dismisses + retries.
    if (isRecordActive()) {
      setStartBlockedByRecord(true);
      return;
    }

    setState('starting');
    setError(null);
    setSaveConfirmation(null);
    setPaidFeaturePrompt(null);
    setStatusText('Starting microphone and VAD');
    setSummary(EMPTY_SUMMARY);
    lastAutoFireRef.current = Date.now();
    setLiveTranscript([]);
    setChunksUploaded(0);
    setSessionsCompleted(0);
    startedAtRef.current = Date.now();
    setSessionStartedAt(new Date().toISOString());
    setElapsedSeconds(0);
    // v3.22.5 — reset stuck-state telemetry. Fresh session = warm-up
    // window restarts; no chunks landed yet.
    lastChunkAtRef.current = null;
    setIsStuck(false);
    // Mint a fresh idempotency key for this start() lifecycle. createSession
    // sends it on /start-always-on so a retried create (double-click /
    // network retry / a re-fired start()) reuses the same server session
    // instead of creating the duplicate row pair the incident produced.
    clientSessionKeyRef.current = uuid();
    // Fresh session — forget which slices we've reported to analytics.
    trackedSliceIdsRef.current = new Set();
    // Re-arm the one-time "live transcript unavailable" status note.
    liveOnlyStTUnavailableNotedRef.current = false;

    // Lock the privacy mode for this session at start. Free-tier users
    // do not have canonical_reprocess, so their effective mode is
    // local-only even when the privacy toggle is off.
    const requiresLocalOnly = localOnlyPref || !canUseServerProcessing;
    if (requiresLocalOnly && !localOnlyAvailable) {
      const message = !canUseServerProcessing
        ? 'Free recordings run on-device and require a WebGPU-capable browser such as current Chrome or Edge. This browser cannot start a free local recording.'
        : 'Privacy Mode needs a WebGPU-capable browser such as current Chrome or Edge. Turn Privacy Mode off or switch browsers to record with server processing.';
      if (!canUseServerProcessing) showPaidFeaturePrompt(message);
      activeLocalOnlyRef.current = false;
      setActiveLocalOnly(false);
      setState('idle');
      setStatusText('Idle');
      setSessionStartedAt(null);
      startedAtRef.current = null;
      setAlwaysOnActive(false);
      setError(message);
      return;
    }
    const useLocalOnly = requiresLocalOnly;
    activeLocalOnlyRef.current = useLocalOnly;
    setActiveLocalOnly(useLocalOnly);

    // Lock the audio source mode for this session. Same locking pattern
    // as privacy mode — a mid-session flip in another tab doesn't half-
    // swap the engine. The ref is read inside the engine opener.
    const lockedMode = getAudioSourceMode();
    activeAudioSourceModeRef.current = lockedMode;
    setAudioSourceModeState(lockedMode);

    // Flag the presence so the explicit Record surfaces can refuse a
    // collision. Cleared in stop() or in the catch branch below.
    setAlwaysOnActive(true);

    try {
      await createSession();
      const engine = await ensureEngine();
      await engine.start();
      // After the VAD engine boots we have an active MediaStream we can
      // share with the full-audio recorder.
      //
      // v3.26.9 LOCAL-then-UPLOAD: the recorder NEVER streams to the
      // server during a recording. It BUFFERS the whole meeting on-device
      // and the Stop path uploads the assembled blob exactly ONCE via
      // /full-audio. Two buffering backends:
      //   * Desktop + IndexedDB available  → persist each ~30s chunk to
      //     IDB (`localPersistence: true`). Survives a tab crash; the
      //     orphan banner re-uploads the buffered blob.
      //   * Mobile / no-IDB                → accumulate parts in memory
      //     (`inMemory: true`). No crash recovery, but the same single
      //     /full-audio upload-at-Stop works on phones.
      // Privacy/local-only adds `localOnly: true` (buffer, never upload).
      const stream = engineRef.current?.getActiveStream?.() ?? activeMicStream;
      const sessionIdForRecord = sessionRef.current;
      const idbAvailable =
        supportsLocalPersistence(deviceCapability)
        && localAudioStore.isAvailable();
      // When IDB isn't the buffer (mobile / desktop-fallback without IDB),
      // accumulate in memory so we still have a whole-file blob to upload
      // at Stop. This is the mobile always-on path.
      const useInMemoryBuffer = !idbAvailable;
      recorderInMemoryRef.current = useInMemoryBuffer;
      if (stream && sessionIdForRecord && (activeOrganization || useLocalOnly)) {
        if (idbAvailable) {
          try {
            await localAudioStore.createSessionMetadata(
              sessionIdForRecord,
              new Date().toISOString(),
              useLocalOnly,
            );
          } catch (err) {
            // Non-fatal — recorder still runs without metadata seeding.
            // eslint-disable-next-line no-console
            console.warn('[always-on] localAudioStore metadata seed failed:', err);
          }
        }
        const recorder = createFullAudioRecorder({
          stream,
          sessionId: sessionIdForRecord,
          apiUrl: config.apiUrl,
          orgSlug: activeOrganization?.slug ?? null,
          timesliceMs: 30_000,
          // v3.x DURABILITY: stream each ~30s chunk to /audio-chunks DURING
          // recording so the server holds chunks 0..N on disk — a backend
          // restart/crash/disconnect leaves a recoverable partial instead of
          // a total loss. IndexedDB stays the desktop crash-recovery mirror
          // (localPersistence) and the orphan banner is unchanged. Privacy /
          // local-only KEEPS bufferOnly:true via `useLocalOnly` so the
          // "nothing leaves the device" invariant holds (the recorder also
          // forces bufferOnly when localOnly is set, but we gate here too so
          // the intent is explicit and a privacy session never streams).
          bufferOnly: useLocalOnly,
          localPersistence: idbAvailable,
          inMemory: useInMemoryBuffer,
          localOnly: useLocalOnly,
        });
        if (recorder) {
          fullAudioRecorderRef.current = recorder;
          recorder.start();
        }
      }
      setState('recording');
      setStatusText('Listening');
      track('recording_started', {
        audio_source_mode: lockedMode,
        device_class: deviceCapability.capabilityClass,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      await engineRef.current?.stop().catch(() => undefined);
      engineRef.current = null;
      sessionRef.current = null;
      setCurrentSessionId(null);
      setSessionStartedAt(null);
      startedAtRef.current = null;
      setAlwaysOnActive(false);
      setState('idle');
      setStatusText('Idle');
      setError(message);
    }
  }, [createSession, ensureEngine, localOnlyPref, localOnlyAvailable, canUseServerProcessing, showPaidFeaturePrompt, deviceCapability, activeOrganization, activeMicStream]);

  /**
   * v3.26.9 — re-attach the full-audio buffer recorder to a NEW stream on
   * a device / source hot-swap. The VAD engine tears down + rebuilds on
   * switchDevice and binds a fresh MediaStream; the old MediaRecorder was
   * bound to the (now-stopped) old stream and would capture nothing after
   * the swap. We stop the old recorder (its buffered chunks are retained —
   * IDB keys by session_id, so getAssembledBlob concatenates across
   * recorder restarts) and start a new one on the new stream that
   * CONTINUES the chunk sequence (startChunkIndex) so it never overwrites
   * the old recorder's chunk 0. On the in-memory (mobile) path we carry
   * the pre-swap audio forward via initialInMemoryParts.
   *
   * Best-effort: any failure just means the post-swap audio segment is
   * missing from the final upload; the live transcript (browser STT) is
   * unaffected because it rides the VAD engine, which already re-bound.
   */
  const reattachFullAudioRecorder = useCallback((stream: MediaStream) => {
    const old = fullAudioRecorderRef.current;
    // Only meaningful mid-session with an existing recorder. The initial
    // start() bind has no recorder yet, so this is a clean no-op there.
    if (!old) return;
    if (stateRef.current !== 'recording' && stateRef.current !== 'paused') return;
    const sessionId = sessionRef.current;
    if (!sessionId) return;
    const useInMemory = recorderInMemoryRef.current;
    const useLocalOnly = activeLocalOnlyRef.current;
    const idbAvailable =
      supportsLocalPersistence(deviceCapability)
      && localAudioStore.isAvailable();
    // Detach the ref immediately so a second swap can't double-stop.
    fullAudioRecorderRef.current = null;
    void (async () => {
      // Flush + stop the old recorder so its trailing slice lands before
      // we read the final chunk count / in-memory parts.
      try {
        await old.flush();
      } catch {
        /* best-effort */
      }
      try {
        await old.stop();
      } catch {
        /* best-effort — old stream is already dead */
      }
      // Continue the IDB sequence from where the old recorder stopped so
      // we don't collide on the composite [session_id, sequence_number].
      const startChunkIndex = old.chunksProduced();
      let carriedParts: Blob[] | undefined;
      if (useInMemory) {
        const carried = old.getAssembledBlob();
        carriedParts = carried ? [carried] : [];
      }
      // If we've already moved past this session (stop/restart raced the
      // swap), don't resurrect a recorder.
      if (stateRef.current !== 'recording' && stateRef.current !== 'paused') return;
      if (sessionRef.current !== sessionId) return;
      const next = createFullAudioRecorder({
        stream,
        sessionId,
        apiUrl: config.apiUrl,
        orgSlug: activeOrganization?.slug ?? null,
        timesliceMs: 30_000,
        // Match the durability flip in start(): keep streaming chunks to the
        // server after a device/source hot-swap (continuing the chunk
        // sequence via startChunkIndex). Privacy/local-only stays buffer-only.
        bufferOnly: useLocalOnly,
        localPersistence: idbAvailable,
        inMemory: useInMemory,
        localOnly: useLocalOnly,
        startChunkIndex,
        initialInMemoryParts: carriedParts,
      });
      if (next) {
        fullAudioRecorderRef.current = next;
        next.start();
      }
    })();
  }, [deviceCapability, activeOrganization]);

  useEffect(() => {
    reattachRecorderRef.current = reattachFullAudioRecorder;
  }, [reattachFullAudioRecorder]);

  const pause = useCallback(async () => {
    if (stateRef.current !== 'recording') return;
    setState('paused');
    setVadActive(false);
    setStatusText('Paused');
    await engineRef.current?.pause();
  }, []);

  const resume = useCallback(async () => {
    if (stateRef.current !== 'paused') return;
    setState('recording');
    setStatusText('Listening');
    await engineRef.current?.resume();
  }, []);

  const stop = useCallback(async () => {
    if (stateRef.current === 'idle' || stateRef.current === 'stopping') return;
    setState('stopping');
    setVadActive(false);
    setStatusText('Stopping');
    // Clear any stale error from earlier in the session (e.g. a single
    // dropped chunk's "Browser STT failed" toast). Otherwise a clean stop
    // leaves a misleading red error sitting above a successfully-saved
    // meeting. A genuine failure during this stop() re-sets it below.
    setError(null);
    setSaveConfirmation(null);
    summarizeAbortRef.current?.abort();

    const sessionId = sessionRef.current;
    const wasLocalOnly = activeLocalOnlyRef.current;
    // Did the server-side finalize complete without throwing? Drives the
    // post-stop "Saved — processing your meeting." confirmation. (The
    // orphan rescan can't re-flag this session regardless, because the
    // server paths now mark the local IDB row finalized before idle.)
    // Stays false for local-only (handled inline) and any path that throws.
    let finalizeSucceeded = false;
    // Local synchronous failure flag — `error` state is captured in this
    // closure and won't reflect a setError() we call below (React batches
    // state), so we can't read `error` in finally to detect a failed stop.
    let stopErrored = false;
    // Snapshot the slices we accumulated before clearing state.
    const slicesSnapshot = [...summary.slices];

    // Whether the active recorder buffered in memory (mobile / no-IDB).
    // Snapshot before we clear the ref so the server branch below knows
    // which getAssembledBlob() to read.
    const recorderUsedInMemory = recorderInMemoryRef.current;
    recorderInMemoryRef.current = false;
    // In-memory assembled blob + its MIME, captured after the recorder
    // flushes its final slice. Null on the desktop IDB path (which reads
    // localAudioStore.getAssembledBlob instead) and when nothing buffered.
    let inMemoryBlob: Blob | null = null;
    let recorderMime = 'audio/webm';

    try {
      // Tear down the full-audio recorder FIRST so it flushes the final
      // slice into its local buffer (and, for non-privacy sessions, streams
      // that last chunk to the server) before we read the assembled blob.
      // recorder.stop() drains the in-flight chunk-upload chain internally,
      // so when it resolves the server has chunks 0..N on disk.
      const recorder = fullAudioRecorderRef.current;
      fullAudioRecorderRef.current = null;
      if (recorder) {
        recorderMime = recorder.mimeType() || recorderMime;
        try {
          await recorder.flush();
        } catch {
          /* flush is best-effort */
        }
        try {
          await recorder.stop();
        } catch {
          /* stop is best-effort — gap in audio is acceptable */
        }
        // Pull the in-memory assembled blob now (post-stop, so the final
        // slice is included). Desktop IDB path returns null here and uses
        // localAudioStore.getAssembledBlob below.
        if (recorderUsedInMemory) {
          try {
            inMemoryBlob = recorder.getAssembledBlob();
          } catch (err) {
            // eslint-disable-next-line no-console
            console.warn('[always-on] in-memory assemble failed:', err);
          }
        }
      }
      await engineRef.current?.stop();
      engineRef.current = null;
      await uploadQueueRef.current;
      if (sessionId) {
        if (wasLocalOnly) {
          // Phase A.6: two-stage local pass at stop.
          //  Stage 1 — live-slice roll-up (pre-A.6 behaviour) so we
          //  always have *some* summary if the user closes the tab
          //  mid-Stage-2 or the full-audio pass blows up.
          //  Stage 2 — Parakeet 0.6B INT8 over the assembled IDB blob
          //  to produce a high-quality transcript, then a second LLM
          //  final-summary pass over that full transcript. Status is
          //  written into the local session row at each step so the
          //  Local Sessions UI can show "running" → "complete" / "failed".
          setStatusText('Generating live-quality summary locally');
          // Snapshot of the assembled audio blob — needed for Stage 2
          // AND for marking the audio MIME / byte total on the session
          // row. Computed once, reused.
          let assembledForPass: Awaited<ReturnType<typeof localAudioStore.getAssembledBlob>> | null = null;
          // Mark the local audio session ended (privacy mode keeps it
          // in IndexedDB — user manages via the Local Sessions surface).
          if (supportsLocalPersistence(deviceCapability)
            && localAudioStore.isAvailable()) {
            try {
              assembledForPass = await localAudioStore.getAssembledBlob(sessionId);
              await localAudioStore.setSessionFinalized(sessionId, assembledForPass.sha256);
            } catch (err) {
              // No chunks (e.g. user stopped before first 30s) — just
              // log and continue. The slice rollup below handles the
              // text-only case.
              // eslint-disable-next-line no-console
              console.debug('[always-on] No local audio to finalize:', err);
            }
          } else if (recorderUsedInMemory && inMemoryBlob && inMemoryBlob.size > 0) {
            // No-IDB privacy device: feed the in-memory buffered blob to
            // the Stage-2 local pass so phones still get a full-audio
            // transcript. (sha256/chunkCount aren't needed downstream.)
            assembledForPass = {
              blob: inMemoryBlob,
              sha256: '',
              bytes: inMemoryBlob.size,
              chunkCount: 1,
            };
          }
          let liveFinalText: string | null = null;
          // Concatenate slice texts oldest-first for the live-slice
          // roll-up. If there are no slices, we still close the session —
          // the user may have just talked for under one summary threshold.
          if (slicesSnapshot.length > 0) {
            const joined = slicesSnapshot
              .map((s, i) => `Slice ${i + 1} (${s.createdAt}):\n${s.text.trim()}`)
              .join('\n\n');
            const modelId = getStoredModelId();
            try {
              const finalController = new AbortController();
              let buffer = '';
              for await (const token of inBrowserLLM.summarizeFinal(joined, modelId, {
                signal: finalController.signal,
                sessionId,
              })) {
                buffer += token;
              }
              liveFinalText = buffer.trim() || null;
            } catch (err) {
              // eslint-disable-next-line no-console
              console.warn('[always-on] Local live-slice final summary failed:', err);
              setError('Local live summary failed; slices are still saved.');
            }
          } else {
            // No slices — the meeting ended before the first slice
            // threshold (~500 words). Summarize the raw live transcript
            // directly so a short local-only meeting still gets a real
            // summary instead of silently ending with none.
            const rawTranscript = transcriptTextRef.current.trim();
            if (rawTranscript && countWords(rawTranscript) >= 30) {
              const modelId = getStoredModelId();
              try {
                const finalController = new AbortController();
                let buffer = '';
                for await (const token of inBrowserLLM.summarizeFinal(rawTranscript, modelId, {
                  signal: finalController.signal,
                  sessionId,
                })) {
                  buffer += token;
                }
                liveFinalText = buffer.trim() || null;
              } catch (err) {
                // eslint-disable-next-line no-console
                console.warn('[always-on] Short-meeting transcript summary failed:', err);
              }
            }
          }
          await endLocalSession(sessionId, liveFinalText);

          // Stage 2 — full-quality Parakeet pass + LLM re-summary. Only
          // attempted when we actually have an assembled blob AND the
          // device can take the load: the whole-meeting pass is far heavier
          // than live inference and hard-froze a tester's MacBook
          // (2026-07-16). When the gate declines, the live summary is kept
          // as the final record and the session row says why.
          const fullPassGate = shouldRunFullLocalPass(
            deviceCapability,
            assembledForPass?.bytes ?? 0,
          );
          if (
            assembledForPass
            && assembledForPass.bytes > 0
            && shouldRunBrowserInference(deviceCapability)
            && !fullPassGate.ok
          ) {
            await markParakeetPassFailed(
              sessionId,
              `Full-quality local pass skipped: ${fullPassGate.reason}. The live summary is the final record for this session.`,
            ).catch(() => undefined);
            setStatusText('Saved with the live summary (full-quality pass skipped on this device).');
          }
          if (
            assembledForPass
            && assembledForPass.bytes > 0
            && shouldRunBrowserInference(deviceCapability)
            && fullPassGate.ok
          ) {
            const assembled = assembledForPass;
            await markParakeetPassRunning(sessionId).catch(() => undefined);
            try {
              // Parakeet load may not have happened yet if the user
              // hadn't enabled browser STT for live captions. Force-load
              // it now (idempotent — second await resolves immediately).
              setStatusText('Loading local STT for full-audio pass');
              await inBrowserSTT.load({
                onProgress: (pct, label) => {
                  // Surface progress as a percent + label so the user
                  // sees movement during a 30-60s first-load. We reuse
                  // statusText since this is a one-shot stop-time pass.
                  const pctTxt = Math.round(pct * 100);
                  setStatusText(
                    label
                      ? `Loading local STT: ${label} (${pctTxt}%)`
                      : `Loading local STT (${pctTxt}%)`,
                  );
                },
              });
              const sttStarted = performance.now();
              // Rough minutes-of-audio from bytes (~0.75 MB/min opus) so the
              // user knows this is a heavier, minutes-long step — not a hang.
              const estMinutes = Math.max(1, Math.round(assembled.bytes / (0.75 * 1024 * 1024)));
              setStatusText(
                `Running full-quality pass on ~${estMinutes} min of audio — processed in short segments, so the page stays usable`,
              );
              // Chunked long-form pass: ~5-min silence-aligned windows with
              // a yielding mel loop. Replaces the one-shot transcribe()
              // whose whole-meeting working set froze a tester's MacBook
              // (2026-07-16) and exceeded Parakeet's ~24-min envelope.
              const fullTranscriptRaw = await inBrowserSTT.transcribeLong(assembled.blob, {
                onProgress: (doneSec, totalSec) => {
                  if (totalSec <= 0) return;
                  const doneMin = Math.floor(doneSec / 60);
                  const totalMin = Math.max(1, Math.round(totalSec / 60));
                  const pct = Math.min(100, Math.round((doneSec / totalSec) * 100));
                  setStatusText(
                    `Transcribing locally — ${doneMin} of ~${totalMin} min (${pct}%)`,
                  );
                },
              });
              const fullTranscript = (fullTranscriptRaw || '').trim();
              const sttElapsedSec = Math.max(
                0,
                Math.round((performance.now() - sttStarted) / 1000),
              );
              // eslint-disable-next-line no-console
              console.info(
                `[always-on] Local Parakeet pass: ${fullTranscript.length} chars in ${sttElapsedSec}s (RTF=${inBrowserSTT.getDiagnostics().lastRTF?.toFixed(3) ?? 'n/a'})`,
              );

              if (!fullTranscript) {
                // Parakeet returned an empty transcript — likely a
                // codec mismatch or silent audio. Fall back to the
                // live-slice summary we already saved.
                await markParakeetPassFailed(
                  sessionId,
                  'Local STT returned an empty transcript. Live-slice summary kept as final.',
                );
              } else {
                setStatusText('Running local summary over full transcript');
                const modelId = getStoredModelId();
                const llmStarted = performance.now();
                const finalController = new AbortController();
                let llmBuffer = '';
                for await (const token of inBrowserLLM.summarizeFinal(
                  fullTranscript,
                  modelId,
                  { signal: finalController.signal, sessionId },
                )) {
                  llmBuffer += token;
                }
                const llmElapsedSec = Math.max(
                  0,
                  Math.round((performance.now() - llmStarted) / 1000),
                );
                const fullSummary = llmBuffer.trim();
                // eslint-disable-next-line no-console
                console.info(
                  `[always-on] Local LLM full-summary: ${fullSummary.length} chars in ${llmElapsedSec}s`,
                );
                if (!fullSummary) {
                  await markParakeetPassFailed(
                    sessionId,
                    'Local summary returned empty output. Live-slice summary kept as final.',
                  );
                } else {
                  await saveParakeetPass(
                    sessionId,
                    fullTranscript,
                    fullSummary,
                    {
                      mimeType: assembled.blob.type || null,
                      bytes: assembled.bytes,
                    },
                  );
                  setStatusText('Done. Session saved locally.');
                }
              }
            } catch (err) {
              const message = err instanceof Error ? err.message : String(err);
              // eslint-disable-next-line no-console
              console.warn('[always-on] Full-audio local pass failed:', message);
              await markParakeetPassFailed(sessionId, message).catch(() => undefined);
              // Friendly toast — the live-slice summary is still
              // saved, the user isn't left with nothing.
              setError(
                "Couldn't finish the full-quality local pass, so your live summary was kept as the final record. "
                + 'This usually means the browser ran out of memory on a long recording — shorter sessions, or the '
                + 'server pass (Pro, non-privacy mode), avoid it.',
              );
            }
          }
        } else {
          if (!canUseServerProcessingRef.current) {
            showPaidFeaturePrompt();
            throw new Error(getServerProcessingError());
          }
          // v3.x DURABILITY: chunks streamed to /audio-chunks DURING the
          // recording, so the server already holds chunks 0..N on disk. The
          // primary Stop path is now /finalize-audio WITH a verification
          // payload (count/bytes/sha of our local mirror) — the server
          // reassembles what it has and either (a) matches → queues the
          // canonical reprocess from the on-disk chunks (NO 2h re-upload),
          // or (b) reports `incomplete` with missing_chunks → we fall back
          // to a whole-blob /full-audio upload. The whole-blob upload is the
          // recovery path, not the primary — that's what keeps the durable-
          // partial benefit AND avoids re-shipping the entire meeting at
          // Stop. Mobile / no-IDB (no local mirror to verify against) goes
          // straight to /full-audio with the in-memory blob as before.
          setStatusText('Finalizing recording');
          const idbActive =
            supportsLocalPersistence(deviceCapability)
            && localAudioStore.isAvailable();

          // Resolve the assembled local mirror (verification payload +
          // fallback blob). On the desktop IDB path this is the IndexedDB
          // buffer; on mobile it's the in-memory blob (no sha/count, so we
          // skip verification there and upload the whole blob directly).
          let assembled: { blob: Blob; sha256: string; bytes: number; chunkCount: number } | null = null;
          let uploadMime = recorderMime;
          if (idbActive) {
            try {
              const localMeta = await localAudioStore.getSessionMetadata(sessionId);
              if (localMeta && localMeta.chunk_count > 0) {
                const a = await localAudioStore.getAssembledBlob(sessionId);
                // Stamp the local row finalized up front so even if the
                // upload/verify is slow the orphan rescan won't fire for it.
                await localAudioStore.setSessionFinalized(sessionId, a.sha256);
                assembled = a;
                uploadMime = localMeta.mime_type || a.blob.type || recorderMime;
              }
            } catch (err) {
              // eslint-disable-next-line no-console
              console.warn('[always-on] reading IDB buffer at stop failed:', err);
            }
          } else if (inMemoryBlob && inMemoryBlob.size > 0) {
            uploadMime = inMemoryBlob.type || recorderMime;
          }

          // Single shared "ship the whole blob" recovery helper. Used when
          // verification reports a mismatch, when verification can't run
          // (no local mirror, e.g. mobile), or when verification throws.
          const fallbackFullAudio = async (blob: Blob | null): Promise<boolean> => {
            if (!blob || blob.size === 0) return false;
            setStatusText('Uploading recording');
            const result = await postFullAudio({
              apiUrl: config.apiUrl,
              orgSlug: sessionOrgSlugRef.current,
              sessionId,
              blob,
              mimeType: uploadMime,
            });
            if (result.reprocessing_started && idbActive) {
              void localAudioStore.wipeSession(sessionId).catch((err) => {
                // eslint-disable-next-line no-console
                console.warn('[always-on] local wipe after upload failed (non-fatal):', err);
              });
            }
            return Boolean(result.reprocessing_started);
          };

          if (assembled) {
            // Desktop: verify the server's streamed chunks against our
            // mirror, then finalize-from-disk (no re-upload) on a match.
            let verified: Awaited<ReturnType<typeof postFinalizeAudioWithVerification>> | null = null;
            try {
              verified = await postFinalizeAudioWithVerification({
                apiUrl: config.apiUrl,
                orgSlug: sessionOrgSlugRef.current,
                sessionId,
                clientChunkCount: assembled.chunkCount,
                clientBytesTotal: assembled.bytes,
                clientSha256: assembled.sha256,
              });
            } catch (err) {
              // Verification call itself failed (network / 5xx). Don't lose
              // the recording — ship the whole buffered blob as recovery.
              // eslint-disable-next-line no-console
              console.warn('[always-on] finalize-audio verification failed; falling back to full-audio upload:', err);
            }
            const verifiedComplete =
              verified
              && (verified.reprocessing_started
                || verified.status === 'complete'
                || verified.reason === 'already_in_progress');
            const verifiedIncomplete =
              verified
              && (verified.status === 'incomplete' || verified.reason === 'no_chunks_uploaded');
            if (verifiedComplete) {
              // Server reassembled the streamed chunks and kicked reprocess —
              // no whole-file re-upload needed. Drop the local buffer.
              if (idbActive) {
                void localAudioStore.wipeSession(sessionId).catch((err) => {
                  // eslint-disable-next-line no-console
                  console.warn('[always-on] local wipe after verify failed (non-fatal):', err);
                });
              }
            } else if (verifiedIncomplete || !verified) {
              // Mismatch / missing chunks / verification couldn't run →
              // ship the whole buffered blob so nothing is silently lost.
              await fallbackFullAudio(assembled.blob);
            }
          } else if (inMemoryBlob && inMemoryBlob.size > 0) {
            // Mobile / no-IDB: no local mirror to compute a sha against.
            // The chunks DID stream, but the whole-blob upload is the
            // simplest correct finalize here.
            await fallbackFullAudio(inMemoryBlob);
          } else {
            // No local mirror AND no in-memory blob. Either nothing was ever
            // captured, or the chunks streamed straight through with no
            // local copy retained. Ask the server to finalize from whatever
            // it has on disk WITHOUT a verification payload (omitting the
            // body is the contract's "no-verify" path: the server reassembles
            // the on-disk chunks and queues reprocess if any exist, restart-
            // agnostic with zero in-memory state). A no-chunks server returns
            // reason:'no_chunks_uploaded' and /finalize below still closes
            // the row cleanly. We deliberately do NOT send count/bytes here —
            // sending zeros would falsely register as a verification mismatch.
            try {
              await fetch(
                `${config.apiUrl}/api/recordings/sessions/${sessionId}/finalize-audio`,
                {
                  method: 'POST',
                  headers: orgHeader(sessionOrgSlugRef.current),
                  credentials: 'include',
                },
              );
            } catch (err) {
              // eslint-disable-next-line no-console
              console.info('[always-on] Stop with no local buffer; finalize-audio probe failed (non-fatal):', err);
            }
          }

          // Finalize the session row (transcript_simple finalization,
          // duration, status=completed). We hit /finalize directly rather
          // than finalizeSession() so we DON'T double-poke /finalize-audio —
          // the audio reprocess was already kicked above (via the verified
          // finalize-from-disk, the /full-audio fallback, or the no-body
          // finalize-audio probe).
          setStatusText('Finalizing');
          const finalizeResp = await fetch(
            `${config.apiUrl}/api/recordings/sessions/${sessionId}/finalize`,
            {
              method: 'POST',
              headers: orgHeader(sessionOrgSlugRef.current),
              credentials: 'include',
            },
          );
          if (!finalizeResp.ok) {
            if (finalizeResp.status === 403) {
              showPaidFeaturePrompt();
              throw new Error(getServerProcessingError());
            }
            const detail = await finalizeResp.text().catch(() => '');
            throw new Error(detail || 'Failed to finalize always-on session.');
          }
          // Mark the local IDB row finalized so the idle orphan rescan
          // never false-flags this cleanly-stopped + uploaded session.
          await markLocalSessionFinalizedSafe(sessionId);
          // Server finalize accepted (no throw) — drives the clean
          // confirmation + suppresses the orphan rescan for this session.
          finalizeSucceeded = true;
        }
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      stopErrored = true;
    } finally {
      const durationSeconds = startedAtRef.current
        ? Math.round((Date.now() - startedAtRef.current) / 1000)
        : 0;
      track('recording_stopped', {
        duration_seconds: durationSeconds,
        transcript_word_count: transcriptWordCountRef.current,
        slice_count: slicesSnapshot.length,
      });
      sessionRef.current = null;
      clientSessionKeyRef.current = null;
      setCurrentSessionId(null);
      setSessionStartedAt(null);
      startedAtRef.current = null;
      // Clean-stop confirmation. The server path sets finalizeSucceeded
      // once /finalize is accepted; local-only always saves via Stage-1
      // endLocalSession (its Stage-2 failures are caught + only soft-warn,
      // never throw). Either way the meeting is safe — surface a positive
      // notice so the user isn't staring at an empty panel (which the
      // orphan banner used to wrongly fill). Skipped if this stop threw a
      // genuine error (setError ran in catch), so we never show "Saved"
      // and a red error at the same time.
      if (sessionId && !stopErrored) {
        if (finalizeSucceeded) {
          setSaveConfirmation('Saved — processing your meeting.');
        } else if (wasLocalOnly) {
          setSaveConfirmation('Saved on this device.');
        }
      }
      setState('idle');
      setStatusText('Idle');
      activeLocalOnlyRef.current = false;
      setActiveLocalOnly(false);
      // Clear the cross-surface presence flag — the Record button is
      // free to start now. Idempotent if it's already cleared.
      setAlwaysOnActive(false);
      // Drop the opened-source ref. The engine called releaseExtras
      // already which invoked our cleanup; this just nukes the dangling
      // reference so the GC can drop it.
      openedSourceRef.current = null;
    }
  }, [finalizeSession, markLocalSessionFinalizedSafe, summary.slices]);

  // Wire stopRef now that stop is defined. Used by ensureEngine's
  // track-ended handler (which is constructed before stop in the file
  // order). Don't include stop in the engine's dep array — re-creating
  // the engine on every stop change would be wrong.
  useEffect(() => {
    stopRef.current = stop;
  }, [stop]);

  const transcriptText = useMemo(
    () => liveTranscript
      .map((segment) => `${segment.speaker ? `${segment.speaker}: ` : ''}${segment.text}`)
      .join('\n')
      .trim(),
    [liveTranscript],
  );

  const transcriptWordCount = useMemo(() => countWords(transcriptText), [transcriptText]);

  useEffect(() => {
    transcriptTextRef.current = transcriptText;
  }, [transcriptText]);

  // Mirror into a ref so stop() (defined above, memoized) reads the
  // live word count at stop-time rather than a stale closure value.
  useEffect(() => {
    transcriptWordCountRef.current = transcriptWordCount;
  }, [transcriptWordCount]);

  // === Server-side slice polling (standard mode) =====================
  //
  // Standard-mode browser always-on sessions now read their slice stack
  // from the server (Qwen 3.6 35B-A3B-Vision quality) instead of running
  // Qwen 3 0.6B INT8 in the browser. Privacy mode keeps the legacy local
  // path below — server polling never fires when activeLocalOnlyRef is
  // true.
  //
  // We mirror the RoomLiveSummary polling pattern: GET every 5s while
  // recording, plus one refresh on session change so a reviewer landing
  // on a paused/stopped session sees the persisted slices.
  const refreshServerSlices = useCallback(async (sessionId: string): Promise<void> => {
    if (activeLocalOnlyRef.current) return;
    sliceFetchAbortRef.current?.abort();
    const controller = new AbortController();
    sliceFetchAbortRef.current = controller;

    try {
      const headers: Record<string, string> = {};
      try {
        const token = window.localStorage?.getItem('access_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
      } catch {
        /* ignore */
      }
      if (activeOrganization?.slug) {
        headers['X-MeetingOps-Org'] = activeOrganization.slug;
      }
      const resp = await fetch(
        `${config.apiUrl}/api/recordings/sessions/${sessionId}/summary-slices`,
        {
          method: 'GET',
          signal: controller.signal,
          headers,
          credentials: 'include',
        },
      );
      if (!resp.ok) {
        // Network blips self-heal on the next tick. We only surface
        // real failures (the user-visible error banner is reserved for
        // genuine issues).
        return;
      }
      const data = (await resp.json()) as {
        slices?: Array<{
          id: string;
          text: string;
          word_count?: number;
          word_range_start?: number;
          word_range_end?: number;
          model?: string;
          provider?: string;
          created_at?: string;
          triggered_by?: string;
        }>;
      };
      const rawSlices = Array.isArray(data.slices) ? data.slices : [];
      if (rawSlices.length === 0) return;
      const normalized: AlwaysOnSummarySlice[] = rawSlices.map((s) => ({
        id: String(s.id || ''),
        text: String(s.text || ''),
        createdAt: s.created_at || new Date().toISOString(),
        transcriptStart: Number(s.word_range_start || 0),
        transcriptEnd: Number(s.word_range_end || s.word_count || 0),
        model: String(s.model || 'server-llm'),
        modelLabel: String(s.model || 'Server LLM'),
        durationMs: 0,
      }));
      const latest = normalized[normalized.length - 1];
      setSummary({
        slices: normalized,
        currentDraft: '',
        lastWordCountAtSummary: latest?.transcriptEnd ?? 0,
        text: latest?.text ?? '',
        paragraphs: latest ? paragraphsFromText(latest.text) : [],
        updatedAt: latest?.createdAt,
      });
      // Fire analytics once per newly-seen slice (the poll re-GETs the
      // whole stack every 5s, so dedup against ids we've already reported).
      const spec = thresholdSpec(getLiveSummaryAutoConfig().threshold);
      for (const slice of normalized) {
        if (!slice.id || trackedSliceIdsRef.current.has(slice.id)) continue;
        trackedSliceIdsRef.current.add(slice.id);
        track('summary_slice_generated', {
          threshold_kind: spec.kind,
          threshold_value: spec.value,
          llm_route: 'server',
        });
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      // eslint-disable-next-line no-console
      console.debug('[always-on] slice refresh failed', err);
    }
  }, [activeOrganization]);

  // Poll the server-rolled slice stack every 5s while recording. Skipped
  // in privacy mode (no server traffic) and when there's no active
  // session id. Also runs once on session change so a reviewer landing
  // on a paused session sees the latest persisted slices.
  useEffect(() => {
    if (!currentSessionId) return;
    if (activeLocalOnlyRef.current) return;
    if (currentSessionId.startsWith('local-')) return;
    // Browser-inference devices summarize on-device during recording, so
    // there are no server-rolled live slices to poll for. We still do the
    // one-shot initial fetch (a reviewer landing on a session sees any
    // previously-persisted server slices), but skip the 5s recording poll.
    void refreshServerSlices(currentSessionId);
    if (allowBrowserInference) return;
    if (state !== 'recording' && state !== 'paused') return;
    const tick = window.setInterval(() => {
      void refreshServerSlices(currentSessionId);
    }, 5_000);
    return () => window.clearInterval(tick);
  }, [currentSessionId, state, refreshServerSlices, allowBrowserInference]);

  const triggerSummarize = useCallback(async () => {
    // v3.26.10 LOCAL-LIVE-SUMMARY: the live summary ALWAYS runs on the
    // on-device LLM now — standard sessions and privacy sessions alike.
    // As of v3.26.9 nothing streams to the server during a recording (the
    // live transcript is browser-STT-only), so the old standard-mode path
    // that POSTed /summary-slices had no server-side transcript to roll
    // and just 422'd: the user saw the x/500 meter advance but no summary
    // appear. The server still produces the CANONICAL summary from the
    // whole-file reprocess at Stop; these slices are the live, ephemeral
    // on-screen gist. The only per-mode difference is persistence: privacy
    // ALSO writes each slice to IndexedDB (no server record), standard
    // sessions don't (the Stop reprocess owns the canonical record).
    // Capture-only devices (no browser inference) get no live summary and
    // fall through to the Stop-time server summary.
    if (!allowBrowserInference) {
      return;
    }
    if (!transcriptText || summarizingRef.current) return;
    const wordCountAtStart = transcriptWordCount;

    summarizeAbortRef.current?.abort();
    const controller = new AbortController();
    summarizeAbortRef.current = controller;
    summarizingRef.current = true;
    setSummarizing(true);
    setModelLoadProgress(0);

    const startedAt = Date.now();
    const modelId = getStoredModelId();
    const previousSlice = summary.slices[summary.slices.length - 1];
    const sliceText = ''; // accumulates tokens
    let text = sliceText;

    setSummary((prev) => ({ ...prev, currentDraft: '' }));

    try {
      for await (const token of inBrowserLLM.summarize(transcriptText, modelId, {
        signal: controller.signal,
        previousSummary: previousSlice?.text,
        sessionId: sessionRef.current ?? undefined,
        onProgress: (report) => {
          setModelLoadProgress(Math.max(0, Math.min(1, report.progress || 0)));
        },
      })) {
        if (controller.signal.aborted) break;
        text += token;
        setSummary((prev) => ({ ...prev, currentDraft: text }));
      }

      if (controller.signal.aborted) {
        setSummary((prev) => ({ ...prev, currentDraft: '' }));
        return;
      }

      const finalText = text.trim();
      if (!finalText) {
        setSummary((prev) => ({ ...prev, currentDraft: '' }));
        return;
      }

      const slice: AlwaysOnSummarySlice = {
        id: uuid(),
        text: finalText,
        createdAt: new Date().toISOString(),
        transcriptStart: previousSlice ? previousSlice.transcriptEnd : 0,
        transcriptEnd: wordCountAtStart,
        model: modelId,
        modelLabel: getModelLabel(modelId),
        durationMs: Date.now() - startedAt,
      };

      // Local-only mode: persist the slice to IndexedDB. Best-effort —
      // a write failure surfaces a toast but doesn't block the live UI.
      if (activeLocalOnlyRef.current && sessionRef.current) {
        appendLocalSlice(sessionRef.current, {
          text: finalText,
          wordRangeStart: slice.transcriptStart,
          wordRangeEnd: slice.transcriptEnd,
          model: modelId,
          createdAt: slice.createdAt,
        }).catch((err) => {
          // eslint-disable-next-line no-console
          console.warn('[always-on] Local slice persist failed:', err);
        });
      }

      setSummary((prev) => {
        const slices = [...prev.slices, slice];
        return {
          slices,
          currentDraft: '',
          lastWordCountAtSummary: wordCountAtStart,
          text: slice.text,
          paragraphs: paragraphsFromText(slice.text),
          updatedAt: slice.createdAt,
        };
      });
      if (!trackedSliceIdsRef.current.has(slice.id)) {
        trackedSliceIdsRef.current.add(slice.id);
        const spec = thresholdSpec(getLiveSummaryAutoConfig().threshold);
        track('summary_slice_generated', {
          threshold_kind: spec.kind,
          threshold_value: spec.value,
          llm_route: 'browser',
        });
      }
      lastAutoFireRef.current = Date.now();
    } catch (err) {
      if (!controller.signal.aborted) {
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
      }
      setSummary((prev) => ({ ...prev, currentDraft: '' }));
    } finally {
      summarizingRef.current = false;
      setSummarizing(false);
      setModelLoadProgress(null);
    }
  }, [
    activeOrganization,
    refreshServerSlices,
    summary.slices,
    transcriptText,
    transcriptWordCount,
    allowBrowserInference,
  ]);

  // Client-side auto-trigger — runs ONLY in privacy/local-only mode.
  // Standard mode delegates to the server's word-count threshold (set
  // via ROOM_SLICE_TRIGGER_WORDS env, default 500 words) and reads
  // slices via the 5s polling loop above. Without this gate, both
  // surfaces would race to fire slices and the user would see double
  // entries.
  useEffect(() => {
    if (state !== 'recording') return;
    if (!autoConfig.enabled) return;
    if (!allowBrowserInference) return; // live summary runs on-device (standard + privacy)

    const spec = thresholdSpec(autoConfig.threshold);
    if (spec.kind === 'words') {
      const sinceLast = transcriptWordCount - summary.lastWordCountAtSummary;
      if (sinceLast < spec.value) return;
    } else {
      const sinceLast = Date.now() - lastAutoFireRef.current;
      if (sinceLast < spec.value) return;
      if (transcriptWordCount === 0) return;
    }
    if (summarizingRef.current) return;
    if (autoDebounceRef.current) return;

    autoDebounceRef.current = window.setTimeout(() => {
      autoDebounceRef.current = null;
      if (stateRef.current !== 'recording') return;
      if (summarizingRef.current) return;
      void triggerSummarize();
    }, 500);

    return () => {
      if (autoDebounceRef.current) {
        window.clearTimeout(autoDebounceRef.current);
        autoDebounceRef.current = null;
      }
    };
  }, [
    activeLocalOnly,
    autoConfig.enabled,
    autoConfig.threshold,
    state,
    summary.lastWordCountAtSummary,
    transcriptWordCount,
    triggerSummarize,
  ]);

  useEffect(() => {
    if (state !== 'recording') return;
    const spec = thresholdSpec(autoConfig.threshold);
    if (spec.kind !== 'time') return;
    if (!autoConfig.enabled) return;
    if (!allowBrowserInference) return; // live summary runs on-device (standard + privacy)
    const tick = window.setInterval(() => {
      const sinceLast = Date.now() - lastAutoFireRef.current;
      if (sinceLast < spec.value) return;
      if (summarizingRef.current) return;
      if (autoDebounceRef.current) return;
      autoDebounceRef.current = window.setTimeout(() => {
        autoDebounceRef.current = null;
        if (stateRef.current !== 'recording') return;
        if (summarizingRef.current) return;
        if (transcriptWordCount === 0) return;
        void triggerSummarize();
      }, 500);
    }, 5_000);
    return () => window.clearInterval(tick);
  }, [
    activeLocalOnly,
    autoConfig.enabled,
    autoConfig.threshold,
    state,
    transcriptWordCount,
    triggerSummarize,
  ]);

  // v3.22.5 — bump the lastChunkAt ref on every chunk arrival. Mirroring
  // the state into a ref keeps the chunk-upload hot path untouched (we
  // don't have to thread a setter through every code path) — the
  // monotonic `chunksUploaded` counter is the trigger.
  useEffect(() => {
    if (chunksUploaded > 0) {
      lastChunkAtRef.current = Date.now();
    }
  }, [chunksUploaded]);

  // v3.22.5 — stuck-state derivation. Active-but-silent for >60s while
  // claiming to be in a recording-ish state means the MediaRecorder
  // pipeline is dead (phone tab eviction, AudioContext closed, engine
  // crashed silently). We tick every 5s and only flip true when the
  // signals line up cleanly — false positives surface a destructive
  // button to the user and Aaron explicitly said "the check has to be
  // tight."
  //
  // We also require *some* warm-up has elapsed (15s minimum) so the
  // button doesn't flicker on right after Start while the engine is
  // still booting and no chunks have landed yet.
  useEffect(() => {
    const claimsActive =
      state === 'recording' || state === 'paused'
      || state === 'starting' || state === 'stopping';
    if (!claimsActive) {
      if (isStuck) setIsStuck(false);
      return;
    }
    // How long with NO captured audio before we call the recording stuck.
    // 15 min is deliberately long: a "chunk" is a VAD-detected *speech* slice,
    // so a normal quiet stretch in a real meeting (someone reading a doc, a
    // thinking pause, waiting for people to join) produces no chunks and must
    // NOT trip this. A genuinely suspended / dead tab leaves a much larger gap
    // and is still caught. Aaron's standing rule here: false positives on a
    // destructive "reset" prompt are the worst outcome — keep this lenient.
    // (Was 60s, which false-positived on ordinary meeting silence.)
    const STUCK_AFTER_MS = 15 * 60_000;
    // The in-browser engine object should exist within this window of Start;
    // a missing engine after it is a genuine desync, not a slow boot.
    const ENGINE_WARMUP_MS = 30_000;
    const check = () => {
      const sessionStart = startedAtRef.current;
      const lastChunk = lastChunkAtRef.current;
      const now = Date.now();
      const elapsedSinceStart = sessionStart ? now - sessionStart : 0;
      // Engine desync: claims active but the in-browser engine is gone. This
      // is a hard failure that won't self-heal — flag it, but give boot a
      // grace window so a slow phone init doesn't false-positive.
      if (!engineRef.current) {
        const stuckNow = elapsedSinceStart >= ENGINE_WARMUP_MS;
        if (stuckNow !== isStuck) setIsStuck(stuckNow);
        return;
      }
      // Otherwise: stuck only if no audio has been captured for a long time,
      // measured from the last chunk — or from session start if one never
      // landed (a silent start is fine until STUCK_AFTER_MS has passed).
      const lastAudioAt = lastChunk ?? sessionStart ?? now;
      const stuckNow = now - lastAudioAt > STUCK_AFTER_MS;
      if (stuckNow !== isStuck) setIsStuck(stuckNow);
    };
    check();
    const tick = window.setInterval(check, 5_000);
    return () => window.clearInterval(tick);
  }, [state, isStuck]);

  useEffect(() => () => {
    summarizeAbortRef.current?.abort();
    transcriptSocketRef.current?.close();
    engineRef.current?.stop().catch(() => undefined);
    fullAudioRecorderRef.current?.stop().catch(() => undefined);
    fullAudioRecorderRef.current = null;
    sliceFetchAbortRef.current?.abort();
    sliceFetchAbortRef.current = null;
    if (autoDebounceRef.current) {
      window.clearTimeout(autoDebounceRef.current);
      autoDebounceRef.current = null;
    }
    // Clean up the opened source (closes AudioContext, stops tab stream).
    if (openedSourceRef.current) {
      try {
        openedSourceRef.current.cleanup();
      } catch {
        // ignore
      }
      openedSourceRef.current = null;
    }
    // Drop the presence flag — if the user navigates away mid-session
    // the next mount shouldn't think a stale always-on session is live.
    setAlwaysOnActive(false);
  }, []);

  // ---------------------------------------------------------------
  // v3.22.5 — Stuck-state recovery on mount.
  // ---------------------------------------------------------------
  //
  // The problem this solves: phones (and any tab the OS evicts hard)
  // can be killed mid-recording WITHOUT firing the unmount cleanup. The
  // ``meetingops.alwayson.active`` flag stays "true" in localStorage,
  // and the next time the user opens the page, every Record surface
  // (LiveRecording / DesktopBrowserRecorder / MobileLiveRecording) sees
  // ``isAlwaysOnActive()=true`` via the cross-surface guard and refuses
  // to start. Aaron hit this on 2026-06-01 — iPhone Chrome got evicted,
  // server-side session 213 was stuck at status=recording for 21 min
  // (the 6h watchdog hadn't fired yet), and Record was blocked on next
  // visit until the row was manually cleared.
  //
  // The recovery: on mount, if the localStorage flag claims an active
  // always-on session but our context just initialized to ``idle`` with
  // no engine / no in-flight session, the previous tab clearly died
  // without cleanup. We probe the server for any active recording
  // sessions in this user's scope; if the server agrees there's nothing
  // running (or the server says the most-recent claimed session is
  // already failed/completed/stopped/has ended_at), we wipe the local
  // claim. If the server probe FAILS (network error / 401 / 5xx) we
  // leave the flag intact — could be a transient blip; user keeps their
  // existing UX, and they can use the "Reset recording state" escape
  // hatch in AlwaysOnControl.
  //
  // We do NOT touch persisted summary slices on disk — those belong to
  // the dead session, not the active claim. The in-memory summary
  // slices are already empty (we just mounted).
  const stuckStateRanRef = useRef(false);
  useEffect(() => {
    if (stuckStateRanRef.current) return;
    if (typeof window === 'undefined') return;
    // Only run once per mount, and only if there's actually a stale
    // claim to investigate.
    if (!isAlwaysOnActive()) return;
    if (stateRef.current !== 'idle') return;
    if (engineRef.current) return;
    if (sessionRef.current) return;
    stuckStateRanRef.current = true;

    // Defer the probe until after the first paint so we don't compete
    // with the rest of the provider's init work.
    const handle = window.setTimeout(() => {
      void (async () => {
        let token: string | null = null;
        try {
          token = window.localStorage?.getItem('access_token') ?? null;
        } catch {
          /* storage disabled — treat as missing token */
        }

        const wipe = (reason: string) => {
          setAlwaysOnActive(false);
          // eslint-disable-next-line no-console
          console.info(
            `[always-on] stuck-state recovery: ${reason}, wiping local claim`,
          );
        };

        if (!token) {
          // No auth token. Either the user just opened the app for the
          // first time and never logged in, OR they got logged out
          // while the flag was set. Either way, the alwayson claim is
          // meaningless without an authenticated session — wipe it so
          // the post-login Record button isn't blocked.
          wipe('no auth token but local flag is set');
          return;
        }

        // Probe the server for any user-scoped sessions still in
        // status=recording. If the server agrees there's nothing
        // running, the local claim is stale. Most-recent first so we
        // can act on the first row that comes back.
        const headers: Record<string, string> = {
          Authorization: `Bearer ${token}`,
        };
        if (activeOrganization?.slug) {
          headers['X-MeetingOps-Org'] = activeOrganization.slug;
        }

        let probeResp: Response;
        try {
          probeResp = await fetch(
            `${config.apiUrl}/api/simple/recording-sessions?limit=25`,
            { method: 'GET', headers, credentials: 'include' },
          );
        } catch (err) {
          // Network blip — leave the flag intact, user can use the
          // escape hatch. We log so future debugging is fast.
          // eslint-disable-next-line no-console
          console.warn(
            '[always-on] stuck-state probe network error; leaving local claim intact:',
            err,
          );
          return;
        }
        if (!probeResp.ok) {
          // 401 / 500 / etc. We can't tell from here whether the user is
          // genuinely recording — fail safe (don't wipe).
          // eslint-disable-next-line no-console
          console.warn(
            `[always-on] stuck-state probe HTTP ${probeResp.status}; leaving local claim intact`,
          );
          return;
        }

        let rows: Array<{
          session_id?: string;
          id?: string;
          status?: string;
          ended_at?: string | null;
        }> = [];
        try {
          const data = await probeResp.json();
          // List endpoint now returns {items,...} (pagination, performance-1/2/4).
          // Must read .items first — otherwise this probe sees 0 rows and can wipe
          // the local claim for a legitimately-recording always-on session.
          rows = Array.isArray(data) ? data : (data?.items ?? data?.sessions ?? []);
        } catch {
          // Bogus response — fail safe.
          return;
        }

        const activeRecording = rows.find((r) => {
          const status = String(r?.status ?? '').toLowerCase();
          // A live row is status='recording' with no ended_at. Anything
          // else (failed / completed / stopped / processing / etc.) is
          // not actively capturing audio — even 'processing' means the
          // server reprocess is running, and the browser surface for
          // that session is already finalized.
          if (status !== 'recording') return false;
          if (r?.ended_at) return false;
          return true;
        });

        if (!activeRecording) {
          wipe(
            `server returned ${rows.length} sessions, none in status=recording without ended_at`,
          );
        } else {
          // Server agrees there's a live row. We can't reasonably
          // recover the React state for it (no audio stream, no
          // engine, no session id wired into the provider) — the
          // localStorage flag is now correct insurance against the
          // explicit Record button stomping on a still-running
          // upstream session. Leave it alone; user explicitly
          // resolves via the Reset button or stop UI elsewhere.
          // eslint-disable-next-line no-console
          console.info(
            `[always-on] stuck-state recovery: server says session ${activeRecording.session_id ?? activeRecording.id} is recording, leaving local claim intact`,
          );
        }
      })();
    }, 250);

    return () => window.clearTimeout(handle);
  }, [activeOrganization]);

  /**
   * Manual escape hatch for the "Reset recording state" button in
   * AlwaysOnControl. Wipes the cross-surface claim, drops any in-memory
   * remnants of an old session, and best-effort POSTs /stop to the
   * server for the claimed session id so the row is marked failed
   * without waiting for the watchdog cron. Safe to call when nothing is
   * actually live — every step is idempotent.
   */
  const resetStuckState = useCallback(async (sessionIdHint?: string | null) => {
    // Best-effort server-side stop. Only fires when we have a session
    // id to address. The endpoint is idempotent and tolerates an
    // already-failed row.
    const candidate = sessionIdHint || sessionRef.current;
    if (candidate && !candidate.startsWith('local-')) {
      try {
        let token: string | null = null;
        try {
          token = window.localStorage?.getItem('access_token') ?? null;
        } catch {
          /* ignore */
        }
        const headers: Record<string, string> = {};
        if (token) headers.Authorization = `Bearer ${token}`;
        if (activeOrganization?.slug) {
          headers['X-MeetingOps-Org'] = activeOrganization.slug;
        }
        await fetch(
          `${config.apiUrl}/api/simple/recording-sessions/${candidate}/stop`,
          { method: 'POST', headers, credentials: 'include' },
        );
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('[always-on] reset stuck state: server stop failed:', err);
      }
    }

    // Tear down any half-alive engine + recorder. These are no-ops if
    // they were never spun up.
    try {
      fullAudioRecorderRef.current?.stop().catch(() => undefined);
    } catch {
      /* ignore */
    }
    fullAudioRecorderRef.current = null;
    recorderInMemoryRef.current = false;
    try {
      await engineRef.current?.stop();
    } catch {
      /* ignore */
    }
    engineRef.current = null;
    if (openedSourceRef.current) {
      try {
        openedSourceRef.current.cleanup();
      } catch {
        /* ignore */
      }
      openedSourceRef.current = null;
    }

    sessionRef.current = null;
    setCurrentSessionId(null);
    setSessionStartedAt(null);
    startedAtRef.current = null;
    setSummary(EMPTY_SUMMARY);
    setLiveTranscript([]);
    setChunksUploaded(0);
    setVadActive(false);
    setState('idle');
    setStatusText('Idle');
    activeLocalOnlyRef.current = false;
    setActiveLocalOnly(false);
    setAlwaysOnActive(false);
    lastChunkAtRef.current = null;
    setIsStuck(false);
    // eslint-disable-next-line no-console
    console.info('[always-on] stuck-state manually reset by user');
  }, [activeOrganization]);

  /**
   * Hard-delete the current session and reset to idle. Server-backed
   * sessions call DELETE /api/recordings/sessions/{id} which cascades
   * to transcript rows + audio chunks on disk + qdrant points. Local-
   * only sessions just drop the IndexedDB record.
   *
   * Engine is stopped first (no chunks-in-flight after the delete) and
   * the DB row is wiped before we surface success — if the cascade
   * delete fails the session stays around and we surface the error.
   */
  const discardSession = useCallback(async () => {
    if (stateRef.current === 'idle') return;
    const sessionId = sessionRef.current;
    const wasLocalOnly = activeLocalOnlyRef.current;

    setState('stopping');
    setVadActive(false);
    setStatusText('Discarding session');
    summarizeAbortRef.current?.abort();

    try {
      // Stop the full-audio recorder BEFORE the VAD engine so we drain
      // its upload queue ahead of the delete request — otherwise a chunk
      // could land on the server immediately after we deleted the session
      // row and recreate the per-session directory.
      const recorder = fullAudioRecorderRef.current;
      fullAudioRecorderRef.current = null;
      recorderInMemoryRef.current = false;
      if (recorder) {
        try {
          await recorder.stop();
        } catch {
          /* tear-down errors are non-fatal during discard */
        }
      }
      // Stop the engine BEFORE we touch the backend — otherwise an
      // in-flight chunk-upload can race the delete and recreate a row.
      await engineRef.current?.stop().catch(() => undefined);
      engineRef.current = null;
      await uploadQueueRef.current;

      if (sessionId) {
        if (wasLocalOnly || isLocalSessionId(sessionId)) {
          await deleteLocalSession(sessionId);
          // Privacy-mode session: also drop the local audio chunks
          // from IDB. The user explicitly asked to discard, so we don't
          // keep the audio around either.
          if (localAudioStore.isAvailable()) {
            await localAudioStore.wipeSession(sessionId).catch(() => undefined);
          }
        } else {
          const res = await fetch(
            `${config.apiUrl}/api/recordings/sessions/${sessionId}`,
            { method: 'DELETE' },
          );
          if (!res.ok) {
            const detail = await res.text().catch(() => '');
            throw new Error(detail || `DELETE failed (${res.status})`);
          }
          // Server delete cascaded the chunks dir — also wipe our
          // local mirror so we don't leak storage.
          if (localAudioStore.isAvailable()) {
            await localAudioStore.wipeSession(sessionId).catch(() => undefined);
          }
        }
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(`Discard failed: ${message}`);
    } finally {
      sessionRef.current = null;
      setCurrentSessionId(null);
      setSessionStartedAt(null);
      startedAtRef.current = null;
      setSummary(EMPTY_SUMMARY);
      setLiveTranscript([]);
      setChunksUploaded(0);
      setState('idle');
      setStatusText('Idle');
      activeLocalOnlyRef.current = false;
      setActiveLocalOnly(false);
      setAlwaysOnActive(false);
      openedSourceRef.current = null;
    }
  }, []);

  // Forward-ref for start() — populated in an effect below since start
  // depends on createSession + ensureEngine which are defined earlier
  // in the file. restartSession needs to wait for stop() before
  // start()ing again, so we go through this ref.
  const startRef = useRef<() => Promise<void>>(async () => undefined);
  useEffect(() => {
    startRef.current = start;
  }, [start]);

  // ---------------------------------------------------------------
  // Phase A.5 — Orphan session detection + resume.
  // ---------------------------------------------------------------

  /** Re-query IDB and refresh the orphan banner. Cheap; called on
   *  mount and after every stop/discard so the banner stays accurate. */
  const refreshOrphanSessions = useCallback(async () => {
    if (!supportsLocalPersistence(deviceCapability)) return;
    if (!localAudioStore.isAvailable()) return;
    try {
      await localAudioStore.init();
      const orphans = await localAudioStore.getOrphanedSessions();
      setOrphanSessions(orphans);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[always-on] orphan scan failed:', err);
      setOrphanSessions([]);
    }
  }, [deviceCapability]);

  // One-shot scan on mount. Captures the "browser crashed mid-meeting"
  // case where the user reopens the tab and we have chunks sitting in
  // IDB with no live session.
  useEffect(() => {
    void refreshOrphanSessions();
  }, [refreshOrphanSessions]);

  /** Upload now — single whole-file upload of a buffered-but-never-
   *  uploaded recording (the tab crashed mid-meeting before Stop).
   *
   *  v3.26.9 LOCAL-then-UPLOAD: nothing ever streamed to the server, so
   *  there is no partial server-side chunk set to verify against. This is
   *  now a flat getAssembledBlob → postFullAudio → /finalize → wipe. The
   *  old verify/missing-chunks/backfill dance is gone. Idempotent; safe
   *  to retry from the banner button. */
  const resumeOrphanSession = useCallback(async (sessionId: string) => {
    if (!canUseServerProcessingRef.current) {
      showPaidFeaturePrompt();
      setError(getServerProcessingError());
      return;
    }
    if (!activeOrganization) {
      setError('Pick an organization before uploading.');
      return;
    }
    if (!localAudioStore.isAvailable()) return;
    setResumingOrphan(sessionId);
    try {
      const meta = await localAudioStore.getSessionMetadata(sessionId);
      if (!meta || meta.chunk_count === 0) {
        // Nothing buffered — just clean up the row.
        await localAudioStore.wipeSession(sessionId);
        await refreshOrphanSessions();
        return;
      }
      const assembled = await localAudioStore.getAssembledBlob(sessionId);
      await localAudioStore.setSessionFinalized(sessionId, assembled.sha256);
      // ONE upload of the whole buffered recording. Server clears any
      // chunk state, writes the blob, and kicks the reprocess pipeline.
      const recovery = await postFullAudio({
        apiUrl: config.apiUrl,
        orgSlug: activeOrganization.slug ?? null,
        sessionId,
        blob: assembled.blob,
        mimeType: meta.mime_type || assembled.blob.type || 'audio/webm',
      });
      if (recovery.reprocessing_started) {
        await localAudioStore.wipeSession(sessionId);
      }
      // Finalize the session row (transcript_simple + duration +
      // status=completed). Best-effort — the reprocess already started.
      try {
        await fetch(
          `${config.apiUrl}/api/recordings/sessions/${sessionId}/finalize`,
          { method: 'POST' },
        );
      } catch {
        /* non-fatal */
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(`Upload failed: ${message.slice(0, 200)}`);
    } finally {
      setResumingOrphan(null);
      await refreshOrphanSessions();
    }
  }, [activeOrganization, refreshOrphanSessions]);

  /** Discard an orphan — drop the local chunks without uploading. We
   *  do NOT touch the server-side row (the user can clean that up
   *  through the Sessions list if they want). */
  const discardOrphanSession = useCallback(async (sessionId: string) => {
    if (!localAudioStore.isAvailable()) return;
    try {
      await localAudioStore.wipeSession(sessionId);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(`Discard orphan failed: ${message.slice(0, 200)}`);
    } finally {
      await refreshOrphanSessions();
    }
  }, [refreshOrphanSessions]);

  // Refresh after stop/discard so the banner clears once the chunks
  // are wiped from IDB. We piggyback on the state transition rather
  // than wiring it into the stop body so the helper stays self-contained.
  useEffect(() => {
    if (state === 'idle') void refreshOrphanSessions();
  }, [state, refreshOrphanSessions]);

  /**
   * Stop the current session normally (so it's saved + finalized) and
   * immediately start a new one. Audio source mode + privacy mode are
   * preserved across the swap; the only thing that changes is the
   * session UUID. If stop() fails, we don't auto-start — the user can
   * see the error and retry.
   */
  const restartSession = useCallback(async () => {
    if (stateRef.current === 'idle') {
      // Nothing live; just start.
      await startRef.current();
      return;
    }
    await stop();
    // Give the engine a tick to fully release the mic + AudioContext
    // before we reopen. Without this, Chromium sometimes 0-channels
    // the new MediaStream for ~50ms.
    await new Promise<void>((resolve) => setTimeout(resolve, 50));
    await startRef.current();
  }, [stop]);

  const value = useMemo<AlwaysOnContextValue>(() => ({
    state,
    vadActive,
    currentSessionId,
    chunksUploaded,
    elapsedSeconds,
    summary,
    liveTranscript,
    error,
    saveConfirmation,
    dismissSaveConfirmation,
    paidFeaturePrompt,
    dismissPaidFeaturePrompt,
    statusText,
    sessionsCompleted,
    summarizing,
    modelLoadProgress,
    autoConfig,
    transcriptWordCount,
    sttMode,
    sttReady,
    sttLoadProgress,
    sttLoadLabel,
    sttBrowserFailed,
    localOnly: activeLocalOnly,
    localOnlyForcedOff,
    activeMicDeviceId,
    activeMicStream,
    switchingDevice,
    switchMicDevice,
    audioSourceMode,
    setAudioSourceMode: updateAudioSourceMode,
    switchAudioSource,
    start,
    stop,
    pause,
    resume,
    triggerSummarize,
    discardSession,
    restartSession,
    sessionStartedAt,
    startBlockedByRecord,
    clearStartBlocked,
    deviceCapability,
    isCaptureOnlyMode,
    orphanSessions,
    resumingOrphan,
    resumeOrphanSession,
    discardOrphanSession,
    isStuck,
    resetStuckState,
  }), [
    state,
    vadActive,
    currentSessionId,
    chunksUploaded,
    elapsedSeconds,
    summary,
    liveTranscript,
    error,
    saveConfirmation,
    dismissSaveConfirmation,
    paidFeaturePrompt,
    dismissPaidFeaturePrompt,
    statusText,
    sessionsCompleted,
    summarizing,
    modelLoadProgress,
    autoConfig,
    transcriptWordCount,
    sttMode,
    sttReady,
    sttLoadProgress,
    sttLoadLabel,
    sttBrowserFailed,
    activeLocalOnly,
    localOnlyForcedOff,
    activeMicDeviceId,
    activeMicStream,
    switchingDevice,
    switchMicDevice,
    audioSourceMode,
    updateAudioSourceMode,
    switchAudioSource,
    start,
    stop,
    pause,
    resume,
    triggerSummarize,
    discardSession,
    restartSession,
    sessionStartedAt,
    startBlockedByRecord,
    clearStartBlocked,
    deviceCapability,
    isCaptureOnlyMode,
    orphanSessions,
    resumingOrphan,
    resumeOrphanSession,
    discardOrphanSession,
    isStuck,
    resetStuckState,
  ]);

  return (
    <AlwaysOnContext.Provider value={value}>
      {children}
    </AlwaysOnContext.Provider>
  );
}

export function useAlwaysOn(): AlwaysOnContextValue {
  const context = useContext(AlwaysOnContext);
  if (!context) {
    throw new Error('useAlwaysOn must be used inside AlwaysOnProvider');
  }
  return context;
}
