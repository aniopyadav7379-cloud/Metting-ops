/**
 * Full-session audio capture for always-on recording (LOCAL-then-UPLOAD).
 *
 * The VAD-driven chunk path (vadEngine.ts) gives the live transcript its
 * low-latency text feed via in-browser STT. THIS module runs a SEPARATE
 * `MediaRecorder` on the same MediaStream that produces a continuous
 * WebM/Opus (or MP4/AAC on Safari) stream — one small chunk every ~30s.
 *
 * v3.26.9 LOCAL-then-UPLOAD rework: NOTHING streams to the server during
 * a recording. The recorder BUFFERS the whole meeting on-device and the
 * caller (AlwaysOnContext) uploads the assembled blob exactly ONCE at
 * Stop via `postFullAudio()` → /full-audio, which fires the canonical
 * server reprocess pipeline (transcribe → diarize → identify →
 * summarize → title → Brigade → Project-Ops). There is no mid-meeting
 * /audio-chunks POST, so a clean Stop can never leave a partially-
 * uploaded server session and the "unprocessed chunks" prompt is gone.
 *
 * Two buffering backends, picked by the caller:
 *   * Desktop (`localPersistence: true`): each ~30s chunk lands in
 *     IndexedDB via `localAudioStore`. Survives a tab crash — the
 *     orphan banner offers "Upload now" against the buffered blob.
 *     `getAssembledBlob` lives on `localAudioStore` for this path.
 *   * Mobile / no-IDB (`inMemory: true`): chunks accumulate in an
 *     in-memory array on the handle. No crash recovery (a killed phone
 *     tab loses the in-RAM buffer), but the same single /full-audio
 *     upload-at-Stop works. Use the handle's own `getAssembledBlob()`.
 *
 * Why a continuous MediaRecorder (not the VAD slices):
 *   * VAD chunks are speech-bounded slices. They're not contiguous
 *     (silence is discarded), so concatenating them loses real timing.
 *   * The MediaRecorder stream is continuous, retains true wall-clock
 *     timing, and is what the server-side Parakeet 1.1B fp16 + pyannote
 *     diarization pipeline needs to do proper speaker matching.
 *
 * Privacy / local-only mode: identical buffering, and the upload step is
 * simply never invoked by the caller. The blob stays on the device for
 * the local STT/LLM pipeline. `localOnly: true` is a belt-and-suspenders
 * guard at this layer too (no network fetch ever fires).
 *
 * Browser support:
 *   * Chrome / Edge / Firefox: WebM/Opus is universally supported.
 *   * Safari: supports `audio/mp4` (AAC) instead of WebM. We probe
 *     MIME types in order and pick the first the browser advertises.
 *     The server's reassembly step decodes any of these via ffmpeg, so
 *     codec drift across users in the same org is a non-issue.
 */
import { localAudioStore } from '../services/localAudioStore';

// Non-Safari MIME order: prefer WebM/Opus (Chrome/Edge/Firefox native).
const MIME_CANDIDATES_NON_SAFARI = [
  // Chrome / Firefox / Edge default. Opus is open + small.
  'audio/webm;codecs=opus',
  'audio/webm',
  // Safari (and the occasional Chromium that ships AAC) fallback.
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  // Older Firefox sometimes only advertises ogg.
  'audio/ogg;codecs=opus',
];

// Safari MIME order: ALWAYS prefer audio/mp4 (AAC) even on Safari 18.4+
// which now advertises WebM/Opus support. The MP4 path has been the
// stable production codec on iOS for years; the new WebM emitter still
// has edge cases around backgrounding + AirPods reconnects we don't
// want to inherit on a phone demo. Server-side ffmpeg reassembly takes
// either, so the user-visible behavior is identical.
const MIME_CANDIDATES_SAFARI = [
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  'audio/aac',
  // Fallbacks for Safari 18.4+ that exposes opus. We probe these last
  // on iOS/macOS Safari so we keep MP4 as the default unless mp4 is
  // genuinely unavailable.
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
];

/** Detect WebKit-family browsers (Safari macOS + iOS, plus all iOS
 * Chrome/Firefox/Edge which are required by App Store policy to wrap
 * WKWebView). We treat them all as Safari for MIME selection because
 * they all use the same MediaRecorder backend.
 *
 * We avoid relying on `navigator.userAgent` Safari sniffing alone
 * because Chrome on iOS also matches; instead we check for the
 * combination that distinguishes WebKit from Blink: no `chrome` global
 * and either iOS UA or macOS-Safari UA. */
function isWebKitFamily(): boolean {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent || '';
  // All iOS browsers are WebKit under the hood (App Store rule until
  // EU/UK browser-engine choice rolls out broadly; the WebKit code
  // path is still the default everywhere as of 2026-Q2).
  const isIOS = /iPhone|iPad|iPod/.test(ua)
    || (/Macintosh/.test(ua) && typeof navigator.maxTouchPoints === 'number' && navigator.maxTouchPoints > 1);
  if (isIOS) return true;
  // Desktop Safari: UA contains "Safari" but NOT "Chrome"/"Chromium"/"Edg".
  const isDesktopSafari = /Safari\//.test(ua) && !/Chrome\/|Chromium\/|Edg\//.test(ua);
  return isDesktopSafari;
}

/** Pick the first MIME type the current browser will actually record in.
 * Returns an empty string when the browser exposes no MediaRecorder at
 * all (jsdom in tests, very old Safari) — callers should refuse to start
 * in that case rather than guess. Safari/iOS get an MP4-first probe
 * order; everyone else gets WebM-first. */
export function pickRecordableMime(): string {
  if (typeof MediaRecorder === 'undefined') return '';
  const candidates = isWebKitFamily() ? MIME_CANDIDATES_SAFARI : MIME_CANDIDATES_NON_SAFARI;
  for (const mime of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(mime)) return mime;
    } catch {
      // Some Safari builds throw on probing; treat as "not supported".
    }
  }
  return '';
}

/** Configuration knobs for the full-audio recorder. */
export interface FullAudioRecorderOptions {
  /** The MediaStream we're capturing from. Same stream as the VAD engine. */
  stream: MediaStream;
  /** Active session id — used in the upload URL. */
  sessionId: string;
  /** Base API URL (e.g. config.apiUrl). */
  apiUrl: string;
  /** Org slug for the X-MeetingOps-Org header (org-scoping). */
  orgSlug: string | null;
  /**
   * First chunk_index this recorder should use. Defaults to 0. Set to a
   * non-zero value when RE-ATTACHING after a device hot-swap so the new
   * recorder continues the IDB sequence instead of overwriting the old
   * recorder's chunk 0 (the IDB key is `[session_id, sequence_number]`).
   * Pass the previous recorder's `chunksProduced()`.
   */
  startChunkIndex?: number;
  /**
   * In-memory parts to seed the buffer with on construction. Used only on
   * the `inMemory` path when RE-ATTACHING after a hot-swap, so the new
   * recorder's getAssembledBlob() still includes audio captured before
   * the swap. Pass the previous recorder's getAssembledBlob() result
   * wrapped in a single-element array.
   */
  initialInMemoryParts?: Blob[];
  /** How often the recorder emits a chunk via dataavailable. Default 30s. */
  timesliceMs?: number;
  /** Max retries per chunk before we surface a non-fatal warning. */
  maxRetries?: number;
  /** Initial backoff between retries (ms). Doubles each retry. Default 1s. */
  initialBackoffMs?: number;
  /**
   * Persist each chunk to IndexedDB (desktop crash-recovery buffer).
   * Recommended on desktop browsers — the buffered blob is what the
   * caller uploads ONCE at Stop via /full-audio, and what the orphan
   * banner re-uploads after a tab crash. Default false; AlwaysOnContext
   * flips this on when `capabilityClass === 'desktop-capable'` and IDB
   * is available.
   */
  localPersistence?: boolean;
  /**
   * v3.26.9 LOCAL-then-UPLOAD: buffer-only during recording. When true
   * the recorder NEVER POSTs /audio-chunks mid-meeting — it only fills
   * its local buffer (IndexedDB when localPersistence, in-memory when
   * inMemory). The caller uploads the whole assembled blob once at Stop.
   * This is the standard desktop + mobile path now. `localOnly` is the
   * stricter privacy variant (no upload at all, ever); `bufferOnly` is
   * "no STREAMING, but a single Stop-time upload is expected".
   */
  bufferOnly?: boolean;
  /**
   * In-memory accumulation backend for environments without IndexedDB
   * (mobile / private-mode Safari). Each dataavailable Blob part is
   * pushed onto an in-RAM array; `getAssembledBlob()` concatenates them.
   * No crash recovery (a killed tab loses the buffer) but the same
   * single /full-audio upload-at-Stop works on phones. Mutually
   * exclusive with localPersistence in practice (the caller picks one
   * based on `localAudioStore.isAvailable()`), though both being set is
   * harmless — IDB persist + an in-RAM mirror.
   */
  inMemory?: boolean;
  /**
   * Local-only / privacy mode. When true the recorder buffers each
   * chunk (IndexedDB and/or in-memory per the flags above) but skips
   * the server upload path entirely. No /audio-chunks POSTs, no
   * retries, nothing leaves the device. Implies bufferOnly. The blob
   * stays on the device for the local STT/LLM pipeline.
   */
  localOnly?: boolean;
  /** Called once per uploaded chunk with the chunk_index that succeeded. */
  onChunkUploaded?: (chunkIndex: number) => void;
  /** Called when a chunk hits maxRetries and is dropped. Non-fatal — the
   *  session keeps recording, the dropped chunk just leaves a gap in the
   *  server-side reassembled audio. */
  onChunkFailed?: (chunkIndex: number, error: Error) => void;
  /** Called once per chunk persisted to IndexedDB. Lets callers update
   *  a "buffered locally" counter even when server upload is off. */
  onChunkPersisted?: (chunkIndex: number, bytes: number) => void;
}

/** Public surface — what AlwaysOnContext holds onto. */
export interface FullAudioRecorderHandle {
  /** Start recording. Idempotent — calling twice is a no-op. */
  start(): void;
  /** Force an in-progress chunk to flush (drains the encoder). Called on stop. */
  flush(): Promise<void>;
  /** Stop recording. Waits for the final dataavailable + upload before
   *  resolving so the caller knows the queue is empty. */
  stop(): Promise<void>;
  /** True when actively recording. */
  isRecording(): boolean;
  /** Number of chunks the recorder has produced (regardless of upload state). */
  chunksProduced(): number;
  /** Bytes persisted locally so far. Cheap, in-memory counter — does
   *  not query IndexedDB. Counts IDB persists and/or in-memory parts. */
  localBytes(): number;
  /** MIME the recorder picked. Useful for the /full-audio upload so
   *  the caller can label the blob correctly. */
  mimeType(): string;
  /**
   * Assemble the in-memory buffered parts into a single Blob. Only
   * meaningful when `inMemory: true` — the desktop IDB path uses
   * `localAudioStore.getAssembledBlob(sessionId)` instead. Returns null
   * when nothing was buffered in memory (e.g. desktop IDB-only path, or
   * a stop before the first dataavailable). Call AFTER `stop()` so the
   * final slice is included.
   */
  getAssembledBlob(): Blob | null;
}

/**
 * Build a parallel full-audio recorder bound to the given stream.
 *
 * Lifecycle: the caller (AlwaysOnContext) constructs one of these per
 * session and tears it down on stop(). The recorder owns its own upload
 * queue (sequential FIFO with retries) so out-of-order ACKs at the
 * server don't matter — each chunk_index is idempotent on the server side.
 */
export function createFullAudioRecorder(
  options: FullAudioRecorderOptions,
): FullAudioRecorderHandle | null {
  const mime = pickRecordableMime();
  if (!mime) {
    // No supported MediaRecorder MIME type — caller should noop and the
    // session still works via the chunks-text path. Reprocess just won't
    // run for this session.
    return null;
  }

  const timeslice = options.timesliceMs ?? 30_000;
  const maxRetries = options.maxRetries ?? 5;
  const initialBackoff = options.initialBackoffMs ?? 1000;
  const localPersistence = options.localPersistence === true || options.localOnly === true;
  const localOnly = options.localOnly === true;
  const inMemory = options.inMemory === true;
  // v3.26.9: when bufferOnly (or localOnly) is set we NEVER POST a chunk
  // mid-recording. The whole assembled blob is uploaded once at Stop.
  const bufferOnly = options.bufferOnly === true || localOnly;

  let recorder: MediaRecorder | null = null;
  let started = false;
  let stopped = false;
  // Continue the chunk sequence when re-attaching after a hot-swap so we
  // don't overwrite the prior recorder's IDB chunk 0.
  let chunkIndex = Math.max(0, Math.floor(options.startChunkIndex ?? 0));
  let localBytesAccumulated = 0;
  // In-memory accumulation buffer for the no-IDB (mobile) path. Each
  // dataavailable Blob part is pushed here in arrival order; the handle's
  // getAssembledBlob() concatenates them at Stop for the single
  // /full-audio upload. Empty (and unused) on the desktop IDB path. Seeded
  // with pre-swap audio on a hot-swap re-attach.
  const inMemoryParts: Blob[] = options.initialInMemoryParts
    ? [...options.initialInMemoryParts]
    : [];
  for (const seed of inMemoryParts) localBytesAccumulated += seed.size;
  // Sequential upload queue. We chain promises so chunks land on the server
  // in arrival order even when the network is flaky. The server is
  // idempotent on chunk_index either way, so out-of-order isn't unsafe —
  // sequential is just easier to reason about.
  let uploadChain: Promise<void> = Promise.resolve();
  let stopResolve: (() => void) | null = null;
  let pendingFlush: Promise<void> | null = null;
  let pendingFlushResolve: (() => void) | null = null;

  const buildHeaders = (): HeadersInit => {
    const headers: Record<string, string> = {};
    // Bearer auth + org header — match the existing chunks endpoints'
    // expectations. The chunks-text endpoint uses these via cookie/JWT
    // auth so /audio-chunks does the same.
    try {
      const token = window.localStorage?.getItem('access_token');
      if (token) headers['Authorization'] = `Bearer ${token}`;
    } catch {
      /* localStorage blocked — fall back to cookie auth */
    }
    if (options.orgSlug) headers['X-MeetingOps-Org'] = options.orgSlug;
    return headers;
  };

  const persistLocally = async (indexAtCreation: number, blob: Blob): Promise<void> => {
    // In-memory accumulation backend (mobile / no-IDB). Cheap synchronous
    // push; getAssembledBlob() concatenates at Stop. Kept separate from
    // the IDB path so a caller can in principle enable both.
    if (inMemory) {
      inMemoryParts.push(blob);
      localBytesAccumulated += blob.size;
      options.onChunkPersisted?.(indexAtCreation, blob.size);
    }
    if (!localPersistence) return;
    if (!localAudioStore.isAvailable()) return;
    try {
      await localAudioStore.appendChunk(options.sessionId, indexAtCreation, blob, mime);
      // Avoid double-counting / double-notifying when the in-memory
      // mirror already accounted for this chunk above.
      if (!inMemory) {
        localBytesAccumulated += blob.size;
        options.onChunkPersisted?.(indexAtCreation, blob.size);
      }
    } catch (err) {
      // Best-effort. We don't surface an error here because the server
      // upload (in non-privacy mode) is still the authoritative copy.
      // In privacy mode a failure means the chunk is genuinely lost,
      // but there's not much we can do at this layer beyond logging.
      // eslint-disable-next-line no-console
      console.warn(
        `[full-audio] Local persist failed for chunk ${indexAtCreation}:`,
        err,
      );
    }
  };

  const uploadChunk = async (
    indexAtCreation: number,
    blob: Blob,
  ): Promise<void> => {
    // v3.x DURABILITY: per-chunk streaming to /audio-chunks is RE-ENABLED
    // for non-privacy always-on. The caller (AlwaysOnContext.start) now
    // passes `bufferOnly: false` so each ~30s chunk lands on the server's
    // on-disk full_audio dir AS IT RECORDS — surviving a backend restart /
    // crash / tab close, with the IndexedDB mirror kept as belt-and-
    // suspenders. The whole-blob /full-audio upload remains the Stop-time
    // fallback (gap recovery / mobile). Do NOT re-disable this for the
    // standard path. Privacy / local-only (`localOnly: true`) still implies
    // `bufferOnly: true` below so nothing leaves the device. When
    // bufferOnly is set we skip the network entirely.
    if (bufferOnly) return;
    const form = new FormData();
    // Browser-side extension hint for the server's _safe_ext switch. We
    // base it on the recorder MIME; server stores under chunk_index.<ext>.
    const ext = mime.includes('mp4')
      ? 'mp4'
      : mime.includes('aac')
        ? 'aac'
        : mime.includes('ogg')
          ? 'ogg'
          : 'webm';
    form.append('chunk', blob, `audio-${indexAtCreation}.${ext}`);
    form.append('chunk_index', String(indexAtCreation));

    let attempt = 0;
    let backoff = initialBackoff;
    // eslint-disable-next-line no-constant-condition
    while (true) {
      try {
        const resp = await fetch(
          `${options.apiUrl}/api/recordings/sessions/${options.sessionId}/audio-chunks`,
          {
            method: 'POST',
            headers: buildHeaders(),
            body: form,
            credentials: 'include',
          },
        );
        if (!resp.ok) {
          // 4xx is permanent — no point retrying a client-side error
          // (e.g. session ended, invalid session_id). Surface as failure
          // immediately so we don't loop forever.
          if (resp.status >= 400 && resp.status < 500) {
            const detail = await resp.text().catch(() => '');
            throw new Error(
              `audio-chunks ${resp.status}: ${detail.slice(0, 200) || 'client error'}`,
            );
          }
          throw new Error(`audio-chunks ${resp.status}`);
        }
        options.onChunkUploaded?.(indexAtCreation);
        return;
      } catch (err) {
        attempt += 1;
        if (attempt > maxRetries) {
          options.onChunkFailed?.(
            indexAtCreation,
            err instanceof Error ? err : new Error(String(err)),
          );
          // Swallow — we never want to halt the recording loop on a
          // failed upload. The dropped chunk just leaves a gap; the
          // server's ffmpeg reassembly tolerates missing indices.
          return;
        }
        // Re-throw client errors immediately (no retry — see above).
        if (err instanceof Error && err.message.startsWith('audio-chunks 4')) {
          options.onChunkFailed?.(indexAtCreation, err);
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, backoff));
        backoff *= 2;
      }
    }
  };

  const queueUpload = (blob: Blob) => {
    const indexAtCreation = chunkIndex;
    chunkIndex += 1;
    // Persist FIRST, then queue server upload. This ordering is what
    // makes "browser tab crashes mid-upload" a recoverable case: the
    // chunk is on disk before the network request even starts.
    uploadChain = uploadChain
      .then(() => persistLocally(indexAtCreation, blob))
      .then(() => uploadChunk(indexAtCreation, blob))
      .catch((err) => {
        // Final safety net — uploadChunk swallows internally, but if
        // something unexpected leaks we don't want to break the chain.
        // eslint-disable-next-line no-console
        console.warn('[full-audio] queue saw unexpected error', err);
      });
  };

  return {
    start(): void {
      if (started || stopped) return;
      started = true;
      try {
        recorder = new MediaRecorder(options.stream, { mimeType: mime });
      } catch (err) {
        // Some Safari builds throw at construction with a more permissive
        // MIME hint; try the bare type one more time.
        try {
          recorder = new MediaRecorder(options.stream);
        } catch (innerErr) {
          // eslint-disable-next-line no-console
          console.warn(
            '[full-audio] MediaRecorder construction failed; full audio path disabled for this session.',
            innerErr,
          );
          started = false;
          return;
        }
      }
      recorder.ondataavailable = (event: BlobEvent) => {
        if (!event.data || event.data.size === 0) return;
        queueUpload(event.data);
        // If we were waiting for a flush, resolve once this dataavailable
        // is queued. The actual upload is already chained.
        if (pendingFlushResolve) {
          const resolve = pendingFlushResolve;
          pendingFlushResolve = null;
          pendingFlush = null;
          resolve();
        }
      };
      recorder.onstop = () => {
        // Final flush already queued via the last dataavailable that
        // fires before onstop. Resolve the outer stop()'s promise after
        // the upload chain drains.
        uploadChain
          .catch(() => undefined)
          .then(() => {
            if (stopResolve) {
              const resolve = stopResolve;
              stopResolve = null;
              resolve();
            }
          });
      };
      recorder.start(timeslice);
    },

    async flush(): Promise<void> {
      // Ask the recorder to dump whatever's in its current encoder
      // buffer as a chunk. Resolves once that chunk is queued (not yet
      // uploaded — but enqueued behind any pending uploads so stop()
      // can wait on uploadChain for full drain).
      if (!recorder || recorder.state !== 'recording') return;
      if (pendingFlush) return pendingFlush;
      pendingFlush = new Promise<void>((resolve) => {
        pendingFlushResolve = resolve;
      });
      try {
        recorder.requestData();
      } catch (err) {
        // requestData throws on Firefox <= 65 if no slice is pending;
        // treat as already-flushed.
        // eslint-disable-next-line no-console
        console.debug('[full-audio] requestData skipped', err);
        if (pendingFlushResolve) {
          const resolve = pendingFlushResolve;
          pendingFlushResolve = null;
          pendingFlush = null;
          resolve();
        }
      }
      return pendingFlush ?? Promise.resolve();
    },

    async stop(): Promise<void> {
      if (stopped) return;
      stopped = true;
      if (!recorder || recorder.state === 'inactive') {
        // Drain whatever's already queued before resolving.
        await uploadChain.catch(() => undefined);
        return;
      }
      const drain = new Promise<void>((resolve) => {
        stopResolve = resolve;
      });
      try {
        recorder.stop();
      } catch (err) {
        // If stop() throws (recorder already inactive), just drain the chain.
        // eslint-disable-next-line no-console
        console.debug('[full-audio] stop() threw, draining queue', err);
        if (stopResolve) {
          const resolve = stopResolve;
          stopResolve = null;
          resolve();
        }
      }
      await drain;
      await uploadChain.catch(() => undefined);
    },

    isRecording(): boolean {
      return Boolean(recorder && recorder.state === 'recording');
    },

    chunksProduced(): number {
      return chunkIndex;
    },

    localBytes(): number {
      return localBytesAccumulated;
    },

    mimeType(): string {
      return mime;
    },

    getAssembledBlob(): Blob | null {
      if (inMemoryParts.length === 0) return null;
      // Raw concat of the MediaRecorder slices — same surface the server
      // ffmpeg reassembly decodes by content. Type carries the recorder
      // MIME so the /full-audio upload labels the blob correctly.
      return new Blob(inMemoryParts, { type: mime || 'application/octet-stream' });
    },
  };
}

// ---------------------------------------------------------------------------
// Verification helpers — called by AlwaysOnContext at stop() time.
// ---------------------------------------------------------------------------

export interface FinalizeAudioVerificationResponse {
  status?: 'complete' | 'incomplete' | string;
  reprocessing_started?: boolean;
  reason?: string;
  session_id?: string;
  server_bytes?: number;
  server_chunks?: number[];
  missing_chunks?: number[];
  expected_chunks?: number;
  job_id?: string;
}

export interface VerifyAudioInput {
  apiUrl: string;
  orgSlug: string | null;
  sessionId: string;
  clientChunkCount: number;
  clientBytesTotal: number;
  clientSha256: string;
}

/**
 * POST a verification payload to /finalize-audio. The server reassembles
 * the chunks it has on disk and compares to (count, bytes, sha). On
 * match it returns `{status: 'complete'}` and queues the reprocess
 * pipeline as usual. On mismatch it returns `{status: 'incomplete'}`
 * with the missing chunk indices so the caller can backfill or fall
 * back to /full-audio.
 *
 * DEPRECATED (v3.26.9 LOCAL-then-UPLOAD): nothing streams mid-meeting
 * anymore, so there is never a partial server-side chunk set to
 * reconcile. The Stop path and the orphan-resume path both upload the
 * whole assembled blob once via {@link postFullAudio} instead. This
 * helper is retained for one release for backward-compat and is no
 * longer called by AlwaysOnContext.
 */
export async function postFinalizeAudioWithVerification(
  input: VerifyAudioInput,
): Promise<FinalizeAudioVerificationResponse> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  try {
    const token = window.localStorage?.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
  } catch {
    /* localStorage blocked — fall back to cookie auth */
  }
  if (input.orgSlug) headers['X-MeetingOps-Org'] = input.orgSlug;

  const resp = await fetch(
    `${input.apiUrl}/api/recordings/sessions/${input.sessionId}/finalize-audio`,
    {
      method: 'POST',
      headers,
      credentials: 'include',
      body: JSON.stringify({
        client_chunk_count: input.clientChunkCount,
        client_bytes_total: input.clientBytesTotal,
        client_sha256: input.clientSha256,
      }),
    },
  );
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '');
    throw new Error(
      `finalize-audio ${resp.status}: ${detail.slice(0, 200) || 'request failed'}`,
    );
  }
  return (await resp.json()) as FinalizeAudioVerificationResponse;
}

/**
 * Upload an entire assembled audio blob to /full-audio. The server
 * replaces any existing reassembled audio for the session and triggers
 * the canonical reprocess pipeline (transcribe → diarize → identify →
 * summarize → title → Brigade → Project-Ops).
 *
 * v3.26.9 LOCAL-then-UPLOAD: this is now the PRIMARY (and only) audio
 * upload for always-on. The Stop path calls it once with the buffered
 * blob; the orphan-resume path calls it once with the IDB blob after a
 * crash. (It was previously only a chunk-gap recovery fallback.)
 */
export async function postFullAudio(input: {
  apiUrl: string;
  orgSlug: string | null;
  sessionId: string;
  blob: Blob;
  mimeType: string;
}): Promise<FinalizeAudioVerificationResponse> {
  const headers: Record<string, string> = {};
  try {
    const token = window.localStorage?.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
  } catch {
    /* localStorage blocked — fall back to cookie auth */
  }
  if (input.orgSlug) headers['X-MeetingOps-Org'] = input.orgSlug;

  const ext = input.mimeType.includes('mp4')
    ? 'mp4'
    : input.mimeType.includes('aac')
      ? 'aac'
      : input.mimeType.includes('ogg')
        ? 'ogg'
        : 'webm';
  const form = new FormData();
  form.append('audio', input.blob, `full-audio.${ext}`);
  const resp = await fetch(
    `${input.apiUrl}/api/recordings/sessions/${input.sessionId}/full-audio`,
    {
      method: 'POST',
      headers,
      credentials: 'include',
      body: form,
    },
  );
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '');
    throw new Error(
      `full-audio ${resp.status}: ${detail.slice(0, 200) || 'request failed'}`,
    );
  }
  return (await resp.json()) as FinalizeAudioVerificationResponse;
}
