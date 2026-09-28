// In-browser Speech-to-Text using Parakeet-TDT 0.6B INT8 (ONNX).
//
// Ports the proven Parakeet-Browser-Spike (Aaron, 2026-05-19, 0.131x RTF on
// M2 Mac WASM) into a service module with the same lifecycle surface as
// inBrowserLLM: load() / transcribe() / isReady() / unload().
//
// Why this module pulls onnxruntime-web at runtime from a CDN instead of
// importing it statically: the workspace pins onnxruntime-web to 1.17.3
// because vad-web 0.0.30 was built against the legacy WASM glue API and
// breaks ("K.getValue is not a function") on any 1.18+ release. Parakeet
// INT8 was validated on 1.26.0 in the spike — we need both runtimes to
// coexist. A dynamic import of ort@1.26 from jsdelivr keeps the bundled
// 1.17.3 untouched for VAD and gives Parakeet the API it expects.
//
// Tested model layout (fetched from HuggingFace at runtime):
//   encoder-int8.onnx           1.4 MB   graph
//   encoder-int8.onnx.data      838 MB   external weights
//   decoder_joint-int8.onnx     ~52 MB   LSTM + joint
//   vocab.txt                   ~92 KB   SentencePiece vocab + blank
//
// Source: huggingface.co/nasedkinpv/parakeet-tdt-0.6b-v3-onnx-int8.
// Bytes are cached in CacheStorage after the first download — same
// pattern transformers.js uses for the LLM models. This replaces an
// earlier approach where the 880MB was baked into the docker image
// (rsync'd manually, lost on clean redeploy from a fresh git checkout).
//
// CRITICAL gotchas captured in memory (see feedback_ort_web_external_data.md
// and feedback_onnx_channels_first_stride.md):
//   - encoder.onnx.data MUST be fetched explicitly and passed via the
//     `externalData` option to InferenceSession.create(). ORT-Web does
//     not auto-mount sidecars.
//   - Encoder output tensor `[1, 1024, T]` is channels-first row-major.
//     Per-frame slice at time t is `encoded[c * T + t]` for c in 0..D-1,
//     NOT `subarray(t*D, (t+1)*D)`. Getting this wrong silently emits
//     zero tokens.

// We talk to ORT-Web through `any` because we deliberately don't statically
// import the package — see the file header. Once loadOrt() resolves we get
// the real namespace at runtime.
type OrtNamespace = any; // typeof import('onnxruntime-web') @ 1.26.x
type OrtSession = any;
type OrtTensor = any;

// CDN ESM bundle. Pinned to 1.26.0 to match the validated spike. Bumping
// this requires re-validating both INT8 inference numerics and the WASM
// surface (NumThreads / SIMD / externalData). Don't pump it casually.
const ORT_WEB_CDN = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.26.0/dist/ort.webgpu.bundle.min.mjs';
const ORT_WEB_WASM_BASE = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.26.0/dist/';

// Model fetch source. Same pattern transformers.js uses for the LLM
// models: pull directly from HuggingFace at first-session, cache in
// CacheStorage forever after. Eliminates the previous "880MB baked into
// the docker image / rsync'd manually / lost on clean redeploy" failure
// mode.
//
// HuggingFace 302-redirects large files to a `cas-bridge.xethub.hf.co`
// CDN. fetch() follows redirects transparently. CORS is reflected per
// Origin (verified 2026-05-19 against meet.magicunicorn.dev) so no
// proxy is needed.
const HF_BASE = 'https://huggingface.co/nasedkinpv/parakeet-tdt-0.6b-v3-onnx-int8/resolve/main';

// Fallback mirror. Disabled in this build — if HF rate-limits (3000
// resolves per 5 min per IP today) become a real production problem,
// upload the four files to a Backblaze B2 bucket and set this to e.g.
// 'https://f005.backblazeb2.com/file/meeting-ops-models/parakeet-0.6b-int8'.
// The fetcher already tries this on HF failure when set; ship the
// switch-flip in a follow-up commit when actually needed.
const MIRROR_BASE: string | null = null;

// Filenames at the HF repo root. Kept as a const so the few uses below
// stay consistent and bumping a filename only requires one edit.
const MODEL_FILES = {
  encoderGraph: 'encoder-int8.onnx',
  encoderData: 'encoder-int8.onnx.data',
  decoder: 'decoder_joint-int8.onnx',
  vocab: 'vocab.txt',
} as const;

// Public-facing label for the model layout. Used by the settings UI to
// show users where the bytes live during first-load.
const MODEL_PUBLIC_PREFIX = `${HF_BASE}/`;

// Stable cache name — bumping it invalidates the on-device blob cache,
// which is the right thing to do whenever we ship a different INT8 export
// or change the source URL (the cache is keyed by URL).
const CACHE_NAME = 'meetops-parakeet-0.6b-int8-hf-v1';

// Model constants — pulled from the spike. These are intrinsic to
// parakeet-tdt-0.6b-v3 + this INT8 export and must not drift.
const VOCAB_SIZE = 8193;
const BLANK_ID = 8192;
const NUM_DURATIONS = 5;
const PRED_HIDDEN = 640;
const PRED_LAYERS = 2;
const ENCODER_DIM = 1024;
const MEL_BINS = 128;

const N_FFT = 512;
const WIN_LENGTH = 400;
const HOP_LENGTH = 160;
const SAMPLE_RATE = 16000;

// Decoder loop safety caps. MAX_DECODE_STEPS scales with the number of
// encoder frames; CONSEC_SAFETY breaks infinite same-frame token storms.
const DECODE_STEP_MULTIPLIER = 10;
const CONSEC_SAFETY = 30;

export interface STTLoadOptions {
  /** 0..1 progress (download + session init combined). */
  onProgress?: (progress: number, label?: string) => void;
  /** Abort the load. Throws AbortError if used. */
  signal?: AbortSignal;
}

export interface STTTranscribeResult {
  text: string;
  /** Audio duration in seconds. */
  durationSeconds: number;
  /** Wall-clock total for mel+enc+dec, in seconds. */
  elapsedSeconds: number;
  /** Real-time factor (elapsed / audio). <1.0 is faster than real time. */
  rtf: number;
}

interface CacheEntry {
  blob: ArrayBuffer;
  fromCache: boolean;
}

export class InBrowserSTT {
  private ort: OrtNamespace | null = null;
  private encoderSession: OrtSession | null = null;
  private decoderSession: OrtSession | null = null;
  private vocab: string[] | null = null;
  private melFb: Float32Array | null = null;
  private hann: Float32Array | null = null;
  private loadPromise: Promise<void> | null = null;
  private ready = false;
  private lastRTF: number | null = null;

  /**
   * Idempotent. Multiple callers can `await load()` concurrently and they
   * all wait on the same underlying promise.
   */
  async load(opts: STTLoadOptions = {}): Promise<void> {
    if (this.ready) return;
    if (this.loadPromise) {
      return this.loadPromise;
    }
    this.loadPromise = this._load(opts).finally(() => {
      this.loadPromise = null;
    });
    return this.loadPromise;
  }

  private async _load(opts: STTLoadOptions): Promise<void> {
    const { onProgress, signal } = opts;
    const reportProgress = (pct: number, label?: string) => {
      try {
        onProgress?.(Math.max(0, Math.min(1, pct)), label);
      } catch {
        // never let UI callback errors abort the load
      }
    };

    reportProgress(0, 'Loading ORT runtime');
    this.ort = await loadOrt();
    if (signal?.aborted) throw new DOMException('aborted', 'AbortError');

    // ORT-Web tuning copied from spike. WebGPU bundle still defaults to
    // WASM for INT8 ops that aren't covered by the WebGPU EP, so the
    // wasm settings matter even when we request WebGPU.
    this.ort.env.wasm.numThreads = Math.min(4, navigator.hardwareConcurrency || 4);
    this.ort.env.wasm.simd = true;
    // Silence ORT's benign "node not assigned to preferred EP" warnings, which
    // the wasm worker prints to stderr (so they surface as red console errors)
    // on every model load. The per-session logSeverityLevel doesn't catch the
    // graph-partition pass; the global env.logLevel does. ('error' = warnings off)
    try { this.ort.env.logLevel = 'error'; } catch { /* older ORT */ }

    // Build mel filterbank + Hann window once. These are pure float math
    // and never change after load.
    this.melFb = buildMelFilterbank(MEL_BINS, N_FFT, SAMPLE_RATE, 0, SAMPLE_RATE / 2);
    this.hann = hannWindow(WIN_LENGTH);

    // Fetch with cache and progress fan-out. Encoder weights are the
    // dominant cost (838MB); we give it the lion's share of the progress
    // bar so the UI doesn't sit at "5%" for two minutes.
    reportProgress(0.02, 'Loading vocab');
    const vocabText = await fetchTextWithFallback(MODEL_FILES.vocab, signal);
    this.vocab = parseVocab(vocabText);
    reportProgress(0.04, 'Loading encoder graph');

    const encGraphBytes = await fetchWithFallback(
      MODEL_FILES.encoderGraph,
      signal,
    );
    reportProgress(0.06, 'Loading encoder weights');

    const encDataBytes = await fetchWithFallbackProgress(
      MODEL_FILES.encoderData,
      signal,
      (downloaded, total) => {
        // 0.06 -> 0.85 is the encoder weights download segment.
        const pct = total > 0 ? downloaded / total : 0;
        reportProgress(0.06 + pct * 0.79, `Encoder weights ${(downloaded / 1e6).toFixed(0)}MB`);
      },
    );

    reportProgress(0.85, 'Loading decoder');
    const decBytes = await fetchWithFallback(MODEL_FILES.decoder, signal);

    if (signal?.aborted) throw new DOMException('aborted', 'AbortError');

    reportProgress(0.88, 'Creating encoder session');
    // Try WebGPU first, fall back to WASM. The spike validated WASM at
    // 0.131x RTF on M2; WebGPU should be at least that fast on the same
    // hardware. ORT picks the first EP that can host every node; if WebGPU
    // can't, it silently falls through to WASM.
    const executionProviders = chooseExecutionProviders();

    this.encoderSession = await this.ort.InferenceSession.create(
      new Uint8Array(encGraphBytes.blob),
      {
        executionProviders,
        graphOptimizationLevel: 'all',
        // Quiet ORT's benign "node not assigned to preferred EP" Warning
        // chatter (it spams the console on every model load). 3 = Error.
        logSeverityLevel: 3,
        externalData: [
          {
            data: new Uint8Array(encDataBytes.blob),
            // Path must match what onnx_external_data_helper baked into
            // the graph at export. For this export it's the basename.
            path: 'encoder-int8.onnx.data',
          },
        ],
      },
    );

    reportProgress(0.96, 'Creating decoder session');
    this.decoderSession = await this.ort.InferenceSession.create(
      new Uint8Array(decBytes.blob),
      {
        executionProviders,
        graphOptimizationLevel: 'all',
        // Quiet ORT's benign "node not assigned to preferred EP" Warning
        // chatter (it spams the console on every model load). 3 = Error.
        logSeverityLevel: 3,
      },
    );

    reportProgress(1, 'Ready');
    this.ready = true;
  }

  isReady(): boolean {
    return this.ready;
  }

  getLastRTF(): number | null {
    return this.lastRTF;
  }

  /**
   * Transcribe a WAV/PCM/encoded-audio Blob. Resamples to 16k mono if the
   * decoded sample rate disagrees (some browsers ignore the AudioContext
   * sampleRate hint). Empty input returns empty string.
   *
   * Single-window path — meant for the live pipeline's short clips. For
   * whole-meeting audio use `transcribeLong()`, which windows the input:
   * Parakeet-TDT 0.6B's long-form envelope is ~24 minutes, and beyond a
   * few minutes the one-shot path's mel loop + encoder working set grow
   * with the meeting (a 90-min single shot froze a tester's MacBook).
   */
  async transcribe(audioBlob: Blob): Promise<string> {
    this.assertReady();
    const arrayBuffer = await audioBlob.arrayBuffer();
    const samples = await decodeAudioToFloat32(arrayBuffer);
    return this.transcribeSamples(samples, { yieldDuringMel: false });
  }

  /**
   * Long-form transcription for whole-meeting audio. Decodes once, then
   * processes the recording in ~`chunkSeconds` windows, each cut at the
   * quietest 100 ms frame near the boundary (usually an inter-sentence
   * pause) so words aren't sliced in half. Between windows we yield a
   * macrotask, and the mel loop itself yields every ~20 s of audio, so
   * the page stays responsive for the whole pass. Also keeps every
   * window inside the model's trained long-form envelope, which makes
   * long-meeting transcripts *more* accurate than the old one-shot pass,
   * not just cheaper.
   *
   * `onProgress(processedSeconds, totalSeconds)` fires after decode (0,
   * total) and after each window. Peak memory is the decoded 16 kHz mono
   * PCM (~3.8 MB/min) plus one window's tensors (~20 MB) — the
   * whole-meeting mel/encoder tensors of the one-shot path are gone.
   */
  async transcribeLong(
    audioBlob: Blob,
    opts: {
      chunkSeconds?: number;
      onProgress?: (processedSeconds: number, totalSeconds: number) => void;
      signal?: AbortSignal;
    } = {},
  ): Promise<string> {
    this.assertReady();
    const chunkSeconds = Math.max(60, opts.chunkSeconds ?? 300);
    const arrayBuffer = await audioBlob.arrayBuffer();
    const samples = await decodeAudioToFloat32(arrayBuffer);
    const totalSec = samples.length / SAMPLE_RATE;
    if (samples.length === 0 || totalSec < 0.05) return '';

    // Short enough for one window — same as transcribe(), but with the
    // yielding mel loop so even a "short" 5-min clip can't stall paints.
    if (totalSec <= chunkSeconds * 1.25) {
      opts.onProgress?.(0, totalSec);
      const text = await this.transcribeSamples(samples, { yieldDuringMel: true });
      opts.onProgress?.(totalSec, totalSec);
      return text;
    }

    const wallStart = performance.now();
    const parts: string[] = [];
    let cursor = 0;
    opts.onProgress?.(0, totalSec);
    while (cursor < samples.length) {
      if (opts.signal?.aborted) throw new DOMException('aborted', 'AbortError');
      const targetEnd = Math.min(samples.length, cursor + chunkSeconds * SAMPLE_RATE);
      const end = targetEnd >= samples.length
        ? samples.length
        : findQuietSplitPoint(samples, targetEnd);
      // subarray = view into the decoded buffer, no copy.
      const window = samples.subarray(cursor, end);
      const text = await this.transcribeSamples(window, { yieldDuringMel: true });
      if (text) parts.push(text);
      cursor = end;
      opts.onProgress?.(cursor / SAMPLE_RATE, totalSec);
      // Macrotask gap between windows: lets the browser paint and run
      // pending handlers so a 2 h pass never reads as a hang.
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    // Stamp the whole-pass RTF (transcribeSamples left the last window's).
    const elapsedSec = (performance.now() - wallStart) / 1000;
    this.lastRTF = elapsedSec / totalSec;
    return parts.join(' ');
  }

  private assertReady(): void {
    if (!this.ready || !this.encoderSession || !this.decoderSession || !this.vocab) {
      throw new Error('InBrowserSTT.transcribe called before load() resolved.');
    }
  }

  /** Shared mel→encoder→TDT-decode core over 16 kHz mono PCM. */
  private async transcribeSamples(
    samples: Float32Array,
    opts: { yieldDuringMel: boolean },
  ): Promise<string> {
    const ort = this.ort!;
    const audioSec = samples.length / SAMPLE_RATE;
    if (samples.length === 0 || audioSec < 0.05) {
      return '';
    }

    const t0 = performance.now();
    const { mel, nFrames } = opts.yieldDuringMel
      ? await computeLogMelAsync(samples, this.melFb!, this.hann!)
      : computeLogMel(samples, this.melFb!, this.hann!);
    const tMel = performance.now() - t0;

    const tEnc0 = performance.now();
    const audioT = new ort.Tensor('float32', mel, [1, MEL_BINS, nFrames]);
    const lenT = new ort.Tensor(
      'int64',
      BigInt64Array.from([BigInt(nFrames)]),
      [1],
    );
    const encOut = await this.encoderSession.run({
      audio_signal: audioT,
      length: lenT,
    });
    const tEnc = performance.now() - tEnc0;

    const encShape = encOut.outputs.dims as number[];
    const tEncFrames = encShape[2];
    const encData = encOut.outputs.data as Float32Array;

    const tDec0 = performance.now();
    const tokens = await this.decodeTDT(encData, tEncFrames);
    const tDec = performance.now() - tDec0;

    const total = tMel + tEnc + tDec;
    const rtf = total / 1000 / audioSec;
    this.lastRTF = rtf;

    // assertReady() guaranteed vocab at every public entry point; the
    // non-null assertion is safe here (TS can't see through the helper).
    return detokenize(tokens, this.vocab!);
  }

  /**
   * Returns metadata about the most recent transcribe() so the settings UI
   * can show observed real-time factor.
   */
  getDiagnostics(): { lastRTF: number | null } {
    return { lastRTF: this.lastRTF };
  }

  async unload(): Promise<void> {
    // ORT-Web doesn't expose dispose() on InferenceSession in 1.26 yet,
    // but dropping references lets the GC release the WASM heap on its
    // next sweep. WebGPU buffers are tied to the session lifetime in the
    // EP, so this is the documented escape hatch.
    this.encoderSession = null;
    this.decoderSession = null;
    this.ready = false;
  }

  // ---------- Decoder ----------
  // TDT loop: at each step we feed (prev_token, lstm_state, encoded[:,:,t])
  // into decoder_joint. The graph emits logits over (vocab + durations).
  // Argmax over the vocab portion picks the token; argmax over durations
  // picks the time-step jump. Blank means no emit; t += max(duration, 1).
  // Non-blank emits, updates LSTM state, advances t by duration (which may
  // be 0 → next token on same frame). The consec safety cap prevents
  // infinite emit-without-time-advance loops on bad audio.
  private async decodeTDT(
    encoded: Float32Array,
    T: number,
  ): Promise<number[]> {
    const ort = this.ort!;
    const D = ENCODER_DIM;

    let state1: OrtTensor = new ort.Tensor(
      'float32',
      new Float32Array(PRED_LAYERS * 1 * PRED_HIDDEN),
      [PRED_LAYERS, 1, PRED_HIDDEN],
    );
    let state2: OrtTensor = new ort.Tensor(
      'float32',
      new Float32Array(PRED_LAYERS * 1 * PRED_HIDDEN),
      [PRED_LAYERS, 1, PRED_HIDDEN],
    );

    let prevToken = BLANK_ID;
    let t = 0;
    const emitted: number[] = [];
    const maxSteps = T * DECODE_STEP_MULTIPLIER;
    let step = 0;
    let consec = 0;

    while (t < T && step < maxSteps) {
      step++;
      const targetsT = new ort.Tensor('int32', new Int32Array([prevToken]), [1, 1]);
      const targetLen = new ort.Tensor('int32', new Int32Array([1]), [1]);

      // Channel-first stride gather: frame t at channel c = encoded[c*T + t].
      // See memory `feedback_onnx_channels_first_stride.md` — getting this
      // wrong silently produces empty transcripts.
      const frameBuf = new Float32Array(D);
      for (let c = 0; c < D; c++) frameBuf[c] = encoded[c * T + t];
      const frameT = new ort.Tensor('float32', frameBuf, [1, D, 1]);

      const out = await this.decoderSession!.run({
        encoder_outputs: frameT,
        targets: targetsT,
        target_length: targetLen,
        input_states_1: state1,
        input_states_2: state2,
      });

      const logits = out.outputs.data as Float32Array;

      // Vocab + duration argmax. We scan both sub-ranges in one pass shape
      // for clarity over micro-optimization; this is < 0.1% of total time.
      let bestTok = 0;
      let bestTokScore = -Infinity;
      for (let i = 0; i < VOCAB_SIZE; i++) {
        if (logits[i] > bestTokScore) {
          bestTokScore = logits[i];
          bestTok = i;
        }
      }
      let bestDur = 0;
      let bestDurScore = -Infinity;
      for (let d = 0; d < NUM_DURATIONS; d++) {
        const v = logits[VOCAB_SIZE + d];
        if (v > bestDurScore) {
          bestDurScore = v;
          bestDur = d;
        }
      }

      if (bestTok === BLANK_ID) {
        t += bestDur > 0 ? bestDur : 1;
        consec = 0;
      } else {
        emitted.push(bestTok);
        prevToken = bestTok;
        state1 = out.output_states_1 as OrtTensor;
        state2 = out.output_states_2 as OrtTensor;
        t += bestDur;
        consec++;
        if (consec > CONSEC_SAFETY) {
          t += 1;
          consec = 0;
        }
      }
    }

    return emitted;
  }
}

// =============================================================
// Utilities
// =============================================================

let ortModulePromise: Promise<OrtNamespace> | null = null;

async function loadOrt(): Promise<OrtNamespace> {
  if (!ortModulePromise) {
    ortModulePromise = (async () => {
      // CDN ESM import. `@vite-ignore` is critical — otherwise Vite tries
      // to resolve the URL at build time and fails (or worse, eagerly
      // bundles 60MB of ORT wasm).
      const mod = await import(/* @vite-ignore */ ORT_WEB_CDN);
      const ort = (mod as any).default ?? mod;
      // Point ORT at the matching WASM glue + binary on the same CDN.
      ort.env.wasm.wasmPaths = ORT_WEB_WASM_BASE;
      return ort;
    })();
  }
  return ortModulePromise;
}

function chooseExecutionProviders(): string[] {
  // WebGPU first when available; ORT will silently fall back to wasm for
  // ops not implemented on the WebGPU EP. On older Safari / Firefox the
  // wasm path is the only one that works.
  const nav = typeof navigator !== 'undefined'
    ? (navigator as Navigator & { gpu?: unknown })
    : undefined;
  if (nav?.gpu) {
    return ['webgpu', 'wasm'];
  }
  return ['wasm'];
}

// ---------- HF + mirror fallback wrappers ----------
// All four model files go through these. Try HF first; if it throws
// (network error, 429 rate-limit, 5xx) and a mirror is configured, try
// the mirror. AbortError is re-thrown without retry — abort means the
// user pulled the plug on the load, not a network failure.

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

async function fetchWithFallback(
  filename: string,
  signal?: AbortSignal,
): Promise<CacheEntry> {
  try {
    return await fetchCached(`${HF_BASE}/${filename}`, signal);
  } catch (hfErr) {
    if (isAbortError(hfErr) || !MIRROR_BASE) throw hfErr;
    console.warn(`[STT] HF fetch failed for ${filename}, trying mirror:`, hfErr);
    return await fetchCached(`${MIRROR_BASE}/${filename}`, signal);
  }
}

async function fetchTextWithFallback(
  filename: string,
  signal?: AbortSignal,
): Promise<string> {
  const entry = await fetchWithFallback(filename, signal);
  return new TextDecoder('utf-8').decode(entry.blob);
}

async function fetchWithFallbackProgress(
  filename: string,
  signal: AbortSignal | undefined,
  onProgress: (downloaded: number, total: number) => void,
): Promise<CacheEntry> {
  try {
    return await fetchCachedWithProgress(
      `${HF_BASE}/${filename}`,
      signal,
      onProgress,
    );
  } catch (hfErr) {
    if (isAbortError(hfErr) || !MIRROR_BASE) throw hfErr;
    console.warn(`[STT] HF fetch failed for ${filename}, trying mirror:`, hfErr);
    return await fetchCachedWithProgress(
      `${MIRROR_BASE}/${filename}`,
      signal,
      onProgress,
    );
  }
}

async function openCache(): Promise<Cache | null> {
  if (typeof caches === 'undefined') return null;
  try {
    return await caches.open(CACHE_NAME);
  } catch {
    return null;
  }
}

async function fetchCached(
  url: string,
  signal?: AbortSignal,
): Promise<CacheEntry> {
  const cache = await openCache();
  if (cache) {
    const hit = await cache.match(url);
    if (hit) {
      const blob = await hit.arrayBuffer();
      return { blob, fromCache: true };
    }
  }
  const res = await fetch(url, { signal });
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url}: HTTP ${res.status}`);
  }
  const blob = await res.arrayBuffer();
  if (cache) {
    try {
      // The Response we put into the cache must be a *fresh* Response over
      // the same ArrayBuffer — we already consumed res.body. Re-wrap the
      // ArrayBuffer with the same headers so subsequent loads are honored.
      await cache.put(
        url,
        new Response(blob, {
          status: 200,
          headers: { 'Content-Type': res.headers.get('Content-Type') || 'application/octet-stream' },
        }),
      );
    } catch {
      /* cache write failure is non-fatal */
    }
  }
  return { blob, fromCache: false };
}

async function fetchCachedWithProgress(
  url: string,
  signal: AbortSignal | undefined,
  onProgress: (downloaded: number, total: number) => void,
): Promise<CacheEntry> {
  const cache = await openCache();
  if (cache) {
    const hit = await cache.match(url);
    if (hit) {
      const blob = await hit.arrayBuffer();
      onProgress(blob.byteLength, blob.byteLength);
      return { blob, fromCache: true };
    }
  }
  const res = await fetch(url, { signal });
  if (!res.ok) {
    throw new Error(`Failed to fetch ${url}: HTTP ${res.status}`);
  }
  const totalHeader = res.headers.get('content-length');
  const total = totalHeader ? parseInt(totalHeader, 10) : 0;

  if (!res.body) {
    const blob = await res.arrayBuffer();
    onProgress(blob.byteLength, blob.byteLength);
    return { blob, fromCache: false };
  }

  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  while (true) {
    if (signal?.aborted) {
      reader.cancel().catch(() => undefined);
      throw new DOMException('aborted', 'AbortError');
    }
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      received += value.byteLength;
      onProgress(received, total);
    }
  }
  const out = new Uint8Array(received);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.byteLength;
  }
  const blob = out.buffer;
  if (cache) {
    try {
      await cache.put(
        url,
        new Response(blob, {
          status: 200,
          headers: { 'Content-Type': res.headers.get('Content-Type') || 'application/octet-stream' },
        }),
      );
    } catch {
      /* non-fatal */
    }
  }
  return { blob, fromCache: false };
}

function parseVocab(text: string): string[] {
  const arr: string[] = new Array(VOCAB_SIZE);
  for (const line of text.split('\n')) {
    if (!line) continue;
    const lastSpace = line.lastIndexOf(' ');
    if (lastSpace < 0) continue;
    const tok = line.slice(0, lastSpace);
    const id = parseInt(line.slice(lastSpace + 1), 10);
    if (!Number.isNaN(id) && id < VOCAB_SIZE) {
      arr[id] = tok;
    }
  }
  return arr;
}

function detokenize(tokenIds: number[], vocab: string[]): string {
  // SentencePiece-style: U+2581 marks word boundary.
  let out = '';
  for (const id of tokenIds) {
    if (id < 0 || id >= VOCAB_SIZE) continue;
    const tok = vocab[id];
    if (!tok || tok.startsWith('<')) continue;
    if (tok.startsWith('▁')) {
      out += (out ? ' ' : '') + tok.slice(1);
    } else {
      out += tok;
    }
  }
  return out.trim();
}

// ---------- Audio decode + resample ----------
async function decodeAudioToFloat32(
  arrayBuffer: ArrayBuffer,
): Promise<Float32Array> {
  const Ctx: typeof AudioContext =
    (window as any).AudioContext || (window as any).webkitAudioContext;
  // Requesting 16000Hz at AudioContext construction is a hint — Chrome
  // honors it, Safari may not. The downstream resample loop covers both.
  const ctx = new Ctx({ sampleRate: SAMPLE_RATE } as AudioContextOptions);
  let audio: AudioBuffer;
  try {
    audio = await ctx.decodeAudioData(arrayBuffer.slice(0));
  } finally {
    // We don't await close() — it returns a Promise but blocking on it
    // before resampling adds latency for no benefit.
    ctx.close().catch(() => undefined);
  }
  let samples: Float32Array;
  if (audio.numberOfChannels === 1) {
    samples = audio.getChannelData(0).slice();
  } else {
    const l = audio.getChannelData(0);
    const r = audio.getChannelData(1);
    samples = new Float32Array(l.length);
    for (let i = 0; i < l.length; i++) samples[i] = (l[i] + r[i]) / 2;
  }
  if (audio.sampleRate !== SAMPLE_RATE) {
    samples = await resampleTo16k(samples, audio.sampleRate);
  }
  return samples;
}

async function resampleTo16k(
  samples: Float32Array,
  fromRate: number,
): Promise<Float32Array> {
  const offline = new OfflineAudioContext(
    1,
    Math.ceil((samples.length * SAMPLE_RATE) / fromRate),
    SAMPLE_RATE,
  );
  const buf = offline.createBuffer(1, samples.length, fromRate);
  buf.copyToChannel(samples, 0);
  const src = offline.createBufferSource();
  src.buffer = buf;
  src.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0).slice();
}

// ---------- Mel feature extraction ----------
function hannWindow(n: number): Float32Array {
  const w = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    w[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
  }
  return w;
}

function hzToMel(f: number): number {
  return 2595 * Math.log10(1 + f / 700);
}

function melToHz(m: number): number {
  return 700 * (Math.pow(10, m / 2595) - 1);
}

function buildMelFilterbank(
  nMels: number,
  nFft: number,
  sr: number,
  fmin: number,
  fmax: number,
): Float32Array {
  const nBins = nFft / 2 + 1;
  const melMin = hzToMel(fmin);
  const melMax = hzToMel(fmax);
  const melPoints = new Float32Array(nMels + 2);
  for (let i = 0; i < nMels + 2; i++) {
    melPoints[i] = melMin + ((melMax - melMin) * i) / (nMels + 1);
  }
  const hzPoints = new Float32Array(nMels + 2);
  for (let i = 0; i < nMels + 2; i++) hzPoints[i] = melToHz(melPoints[i]);
  const binPoints = new Float32Array(nMels + 2);
  for (let i = 0; i < nMels + 2; i++) binPoints[i] = (hzPoints[i] * nFft) / sr;

  const fb = new Float32Array(nMels * nBins);
  for (let m = 0; m < nMels; m++) {
    const left = binPoints[m];
    const center = binPoints[m + 1];
    const right = binPoints[m + 2];
    for (let k = 0; k < nBins; k++) {
      let w = 0;
      if (k >= left && k <= center) w = (k - left) / (center - left || 1);
      else if (k >= center && k <= right) w = (right - k) / (right - center || 1);
      fb[m * nBins + k] = w;
    }
  }
  return fb;
}

// In-place radix-2 FFT (Cooley-Tukey). Length must be a power of two.
function fft(re: Float32Array, im: Float32Array): void {
  const n = re.length;
  let j = 0;
  for (let i = 1; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      const tr = re[i]; re[i] = re[j]; re[j] = tr;
      const ti = im[i]; im[i] = im[j]; im[j] = ti;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wRe = Math.cos(ang);
    const wIm = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let curRe = 1;
      let curIm = 0;
      for (let k = 0; k < len / 2; k++) {
        const aRe = re[i + k];
        const aIm = im[i + k];
        const bRe = re[i + k + len / 2] * curRe - im[i + k + len / 2] * curIm;
        const bIm = re[i + k + len / 2] * curIm + im[i + k + len / 2] * curRe;
        re[i + k] = aRe + bRe;
        im[i + k] = aIm + bIm;
        re[i + k + len / 2] = aRe - bRe;
        im[i + k + len / 2] = aIm - bIm;
        const nRe = curRe * wRe - curIm * wIm;
        const nIm = curRe * wIm + curIm * wRe;
        curRe = nRe;
        curIm = nIm;
      }
    }
  }
}

// Mel extraction is split into shared per-frame helpers so the sync path
// (live short clips) and the yielding path (transcribeLong windows) run
// byte-identical math and can't drift.

/** librosa-equivalent center=True reflect padding (NeMo default). */
function melPadReflect(samples: Float32Array): Float32Array {
  const pad = N_FFT / 2;
  const padded = new Float32Array(samples.length + 2 * pad);
  for (let i = 0; i < pad; i++) padded[i] = samples[pad - i] || 0;
  padded.set(samples, pad);
  for (let i = 0; i < pad; i++) {
    padded[pad + samples.length + i] = samples[samples.length - 2 - i] || 0;
  }
  return padded;
}

/** One STFT frame → mel bins, written into mel[:, f]. re/im are scratch. */
function melFrameInto(
  padded: Float32Array,
  f: number,
  nFrames: number,
  re: Float32Array,
  im: Float32Array,
  hann: Float32Array,
  melFb: Float32Array,
  mel: Float32Array,
): void {
  const nBins = N_FFT / 2 + 1;
  const start = f * HOP_LENGTH;
  re.fill(0);
  im.fill(0);
  const offset = (N_FFT - WIN_LENGTH) >> 1;
  for (let i = 0; i < WIN_LENGTH; i++) {
    re[offset + i] = padded[start + i] * hann[i];
  }
  fft(re, im);
  for (let m = 0; m < MEL_BINS; m++) {
    let acc = 0;
    const off = m * nBins;
    for (let k = 0; k < nBins; k++) {
      const p = re[k] * re[k] + im[k] * im[k];
      acc += melFb[off + k] * p;
    }
    // NeMo v3 default: log(x + 1e-5).
    mel[m * nFrames + f] = Math.log(acc + 1e-5);
  }
}

/**
 * "per_feature" normalization: zero-mean unit-variance per mel bin.
 * Matches NeMo's audio_to_mel_spectrogram_preprocessor default — what
 * the encoder was trained with.
 */
function normalizePerFeature(mel: Float32Array, nFrames: number): void {
  for (let m = 0; m < MEL_BINS; m++) {
    let sum = 0;
    for (let f = 0; f < nFrames; f++) sum += mel[m * nFrames + f];
    const mean = sum / nFrames;
    let varSum = 0;
    for (let f = 0; f < nFrames; f++) {
      const d = mel[m * nFrames + f] - mean;
      varSum += d * d;
    }
    const std = Math.sqrt(varSum / nFrames) + 1e-5;
    for (let f = 0; f < nFrames; f++) {
      mel[m * nFrames + f] = (mel[m * nFrames + f] - mean) / std;
    }
  }
}

function computeLogMel(
  samples: Float32Array,
  melFb: Float32Array,
  hann: Float32Array,
): { mel: Float32Array; nFrames: number } {
  const padded = melPadReflect(samples);
  const nFrames = 1 + Math.floor((padded.length - N_FFT) / HOP_LENGTH);
  const re = new Float32Array(N_FFT);
  const im = new Float32Array(N_FFT);
  const mel = new Float32Array(MEL_BINS * nFrames);
  for (let f = 0; f < nFrames; f++) {
    melFrameInto(padded, f, nFrames, re, im, hann, melFb, mel);
  }
  normalizePerFeature(mel, nFrames);
  return { mel, nFrames };
}

/**
 * Same math as computeLogMel, but yields a macrotask every ~20 s of audio
 * (2000 hops) so a multi-minute window never blocks the main thread for
 * more than a couple hundred ms at a time. The sync mel loop was the
 * single biggest freeze source in the old whole-meeting pass: 90 min of
 * audio is ~540k frames of FFT+mel in one uninterruptible loop.
 */
async function computeLogMelAsync(
  samples: Float32Array,
  melFb: Float32Array,
  hann: Float32Array,
): Promise<{ mel: Float32Array; nFrames: number }> {
  const YIELD_EVERY_FRAMES = 2000;
  const padded = melPadReflect(samples);
  const nFrames = 1 + Math.floor((padded.length - N_FFT) / HOP_LENGTH);
  const re = new Float32Array(N_FFT);
  const im = new Float32Array(N_FFT);
  const mel = new Float32Array(MEL_BINS * nFrames);
  for (let f = 0; f < nFrames; f++) {
    if (f > 0 && f % YIELD_EVERY_FRAMES === 0) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    melFrameInto(padded, f, nFrames, re, im, hann, melFb, mel);
  }
  normalizePerFeature(mel, nFrames);
  return { mel, nFrames };
}

/**
 * Pick a low-energy sample index to cut a long recording at, searching
 * the 15 s that end at targetEnd. Cutting at the quietest 100 ms frame
 * (usually an inter-sentence pause) instead of an arbitrary offset
 * avoids slicing a word in half at every window boundary.
 */
function findQuietSplitPoint(samples: Float32Array, targetEnd: number): number {
  const SEARCH_BACK_SEC = 15;
  const FRAME = Math.floor(SAMPLE_RATE / 10); // 100 ms energy frames
  const searchStart = Math.max(0, targetEnd - SEARCH_BACK_SEC * SAMPLE_RATE);
  if (targetEnd - searchStart < FRAME * 2) return targetEnd;
  let bestIdx = targetEnd;
  let bestEnergy = Infinity;
  for (let start = searchStart; start + FRAME <= targetEnd; start += FRAME) {
    let acc = 0;
    for (let i = start; i < start + FRAME; i++) acc += samples[i] * samples[i];
    if (acc < bestEnergy) {
      bestEnergy = acc;
      bestIdx = start + (FRAME >> 1);
    }
  }
  return bestIdx;
}

// Singleton, mirrors inBrowserLLM export shape.
export const inBrowserSTT = new InBrowserSTT();

// Provenance tag posted alongside transcribed text. Backend stores it in
// processing_metadata so analytics can split tier reports later.
export const CLIENT_STT_PROVENANCE = 'client-parakeet-0.6b-int8' as const;

export const STT_MODEL_PUBLIC_PREFIX = MODEL_PUBLIC_PREFIX;
