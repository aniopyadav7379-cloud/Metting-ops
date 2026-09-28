import type {
  AppConfig,
  InitProgressReport,
  MLCEngine,
  ModelRecord,
} from '@mlc-ai/web-llm';
import { config } from '../config';

export const IN_BROWSER_LLM_MODEL_KEY = 'meetingops.inBrowserLLM.model.v1';
export const IN_BROWSER_LLM_DTYPE_KEY = 'meetingops.inBrowserLLM.dtype.v1';
// Default model when fresh install / no choice persisted. Qwen 3 0.6B
// q4f16 lands at ~570MB, ~11s cold load, ~43 tok/s on M2 Mac with
// WebGPU; structured prompt captures ~90% of facts. Premium-tier Gemma
// 4 E2B (~3GB) stays as a manual upgrade.
export const DEFAULT_MODEL_ID = 'qwen3-0.6b-onnx-q4f16';

export const LIVE_SUMMARY_AUTO_KEY = 'meetingops.liveSummary.autoConfig.v1';

export type LiveSummaryThreshold =
  | 'words_200'
  | 'words_500'
  | 'minute_1'
  | 'minutes_5';

export interface LiveSummaryAutoConfig {
  enabled: boolean;
  threshold: LiveSummaryThreshold;
}

export const DEFAULT_AUTO_CONFIG: LiveSummaryAutoConfig = {
  enabled: true,
  threshold: 'words_500',
};

export const LIVE_SUMMARY_THRESHOLDS: Array<{
  id: LiveSummaryThreshold;
  label: string;
  kind: 'words' | 'time';
  value: number;
}> = [
  { id: 'words_200', label: 'Every 200 words', kind: 'words', value: 200 },
  { id: 'words_500', label: 'Every 500 words', kind: 'words', value: 500 },
  { id: 'minute_1', label: 'Every 1 minute', kind: 'time', value: 60_000 },
  { id: 'minutes_5', label: 'Every 5 minutes', kind: 'time', value: 300_000 },
];

export function getLiveSummaryAutoConfig(): LiveSummaryAutoConfig {
  if (typeof window === 'undefined') return DEFAULT_AUTO_CONFIG;
  const raw = window.localStorage.getItem(LIVE_SUMMARY_AUTO_KEY);
  if (!raw) return DEFAULT_AUTO_CONFIG;
  try {
    const parsed = JSON.parse(raw) as Partial<LiveSummaryAutoConfig>;
    const valid = LIVE_SUMMARY_THRESHOLDS.some((t) => t.id === parsed.threshold);
    return {
      enabled: parsed.enabled ?? DEFAULT_AUTO_CONFIG.enabled,
      threshold: valid ? (parsed.threshold as LiveSummaryThreshold) : DEFAULT_AUTO_CONFIG.threshold,
    };
  } catch {
    return DEFAULT_AUTO_CONFIG;
  }
}

export function setLiveSummaryAutoConfig(config: LiveSummaryAutoConfig): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(LIVE_SUMMARY_AUTO_KEY, JSON.stringify(config));
  try {
    window.dispatchEvent(
      new CustomEvent<LiveSummaryAutoConfig>('meetingops:live-summary-auto-config', {
        detail: config,
      }),
    );
  } catch { /* noop */ }
}

export interface SummaryParagraph {
  text: string;
  timestamp: string;
}

export interface SummaryResult {
  text: string;
  paragraphs: SummaryParagraph[];
}

// Two parallel browser runtimes coexist behind the same singleton.
// Picker chooses one; AlwaysOnContext is runtime-agnostic.
export type ModelRuntime = 'web-llm' | 'transformers.js';

// Quantization options exposed by transformers.js. web-llm pre-quantizes
// each model record so dtype is fixed there.
export type Dtype = 'q4' | 'q4f16' | 'q8' | 'fp16';

export interface InBrowserModelOption {
  id: string;
  actualModelId: string | null;
  label: string;
  runtime: ModelRuntime | 'server';
  repoId?: string;
  defaultDtype?: Dtype;
  approxSizeMB?: number;
  contextWindow?: number;
  notes?: string;
  vramRequiredMB?: number;
  lowResourceRequired?: boolean;
  source: 'curated' | 'web-llm' | 'server';
}

export interface WebGPUStatus {
  state: 'ready' | 'limited' | 'unavailable';
  label: string;
  detail: string;
  forcedServer: boolean;
}

export interface SummarizeOptions {
  signal?: AbortSignal;
  onToken?: (token: string) => void;
  onProgress?: (report: InitProgressReport) => void;
  previousSummary?: string;
  sessionId?: string;
}

export interface PreloadOptions {
  signal?: AbortSignal;
  onProgress?: (report: InitProgressReport) => void;
  dtype?: Dtype;
}

export interface Summarizer {
  summarize(
    transcript: string,
    modelId: string,
    opts?: SummarizeOptions,
  ): AsyncIterable<string>;
  preloadModel(modelId: string, opts?: PreloadOptions): Promise<void>;
}

// Curated catalog. Order = picker order (newest/best first). Entries with
// runtime: 'transformers.js' load via @huggingface/transformers; web-llm
// entries route through MLC. Server entry is a special sentinel handled by
// the dispatcher.
interface CuratedEntry {
  id: string;
  label: string;
  runtime: ModelRuntime;
  // For web-llm: the prebuilt MLC model_id.
  // For transformers.js: the HuggingFace repo id.
  modelRef: string;
  defaultDtype?: Dtype;
  approxSizeMB?: number;
  contextWindow?: number;
  notes?: string;
}

const CURATED_MODELS: CuratedEntry[] = [
  // ---- transformers.js (ONNX + WebGPU) ----
  // Multimodal Gemma 4 used in text-only mode for summarization. The
  // model has audio + vision encoders on disk but those sessions are
  // skipped at load time when use_external_data_format is honored and we
  // route through AutoModelForImageTextToText with text-only inputs.
  // Total q4f16 download with encoders is ~3GB; pure text decoder path
  // is ~1.5GB cached.
  {
    id: 'qwen3-0.6b-onnx-q4f16',
    label: 'Qwen 3 0.6B ONNX (q4f16 · ~570MB)',
    runtime: 'transformers.js',
    modelRef: 'onnx-community/Qwen3-0.6B-ONNX',
    defaultDtype: 'q4f16',
    approxSizeMB: 570,
    contextWindow: 32768,
    notes: 'Default. Fastest cold-start. Thinking mode is off — structured prompt only.',
  },
  {
    id: 'gemma-4-e2b-q4f16',
    label: 'Gemma 4 E2B (q4f16 · ~3GB)',
    runtime: 'transformers.js',
    modelRef: 'onnx-community/gemma-4-E2B-it-ONNX',
    defaultDtype: 'q4f16',
    approxSizeMB: 3200,
    contextWindow: 8192,
    notes: 'Premium quality. Multimodal model loaded text-only via AutoModelForCausalLM (vision+audio encoders skipped at load when possible).',
  },
  {
    id: 'smollm2-1.7b-onnx-q4f16',
    label: 'SmolLM2 1.7B Instruct ONNX (q4f16 · ~1GB)',
    runtime: 'transformers.js',
    modelRef: 'HuggingFaceTB/SmolLM2-1.7B-Instruct',
    defaultDtype: 'q4f16',
    approxSizeMB: 1060,
    contextWindow: 8192,
    notes: 'HuggingFaceTB official ONNX build.',
  },
  // ---- web-llm (MLC prebuilt) ----
  // Newest first. Entries whose modelRef isn't published by web-llm
  // are silently filtered out at runtime (see records.find check below),
  // so we can stage future variants here optimistically — they appear in
  // the picker the moment web-llm ships them.
  {
    id: 'gemma-4-e2b-it',
    label: 'Gemma 4 E2B (web-llm)',
    runtime: 'web-llm',
    modelRef: 'gemma4-E2B-it-q4f16_1-MLC',
  },
  {
    id: 'qwen3.6-1.7b',
    label: 'Qwen 3.6 1.7B (web-llm)',
    runtime: 'web-llm',
    modelRef: 'Qwen3.6-1.7B-q4f16_1-MLC',
  },
  {
    id: 'qwen3-1.7b',
    label: 'Qwen 3 1.7B (web-llm)',
    runtime: 'web-llm',
    modelRef: 'Qwen3-1.7B-q4f16_1-MLC',
  },
  {
    id: 'qwen3-0.6b',
    label: 'Qwen 3 0.6B (web-llm, smallest)',
    runtime: 'web-llm',
    modelRef: 'Qwen3-0.6B-q4f16_1-MLC',
  },
  {
    id: 'gemma-3-1b-it',
    label: 'Gemma 3 1B IT (web-llm)',
    runtime: 'web-llm',
    modelRef: 'gemma3-1b-it-q4f16_1-MLC',
  },
  {
    id: 'qwen2.5-1.5b-instruct',
    label: 'Qwen 2.5 1.5B Instruct (web-llm)',
    runtime: 'web-llm',
    modelRef: 'Qwen2.5-1.5B-Instruct-q4f16_1-MLC',
  },
  {
    id: 'phi-3.5-mini-instruct',
    label: 'Phi 3.5 Mini Instruct (web-llm, best quality)',
    runtime: 'web-llm',
    modelRef: 'Phi-3.5-mini-instruct-q4f16_1-MLC',
  },
];

let webLLMModulePromise: Promise<typeof import('@mlc-ai/web-llm')> | null = null;
let cachedModelRecords: ModelRecord[] | null = null;

async function loadWebLLM(): Promise<typeof import('@mlc-ai/web-llm')> {
  if (!webLLMModulePromise) {
    webLLMModulePromise = import('@mlc-ai/web-llm').then((mod) => {
      cachedModelRecords = mod.prebuiltAppConfig.model_list.filter(
        (record) => (record.model_type ?? 0) === 0,
      );
      return mod;
    });
  }
  return webLLMModulePromise;
}

function llmRecords(): ModelRecord[] {
  return cachedModelRecords ?? [];
}

function findRecord(modelId: string): ModelRecord | undefined {
  return llmRecords().find((record) => record.model_id === modelId);
}

function displayName(modelId: string): string {
  return modelId
    .replace(/-MLC.*$/, '')
    .replace(/q[0-9]f[0-9]+(?:_[0-9])?/gi, '')
    .replace(/[-_]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function getWebGPUStatus(): WebGPUStatus {
  if (typeof navigator === 'undefined') {
    return {
      state: 'unavailable',
      label: 'Unavailable',
      detail: 'Browser APIs are not available.',
      forcedServer: true,
    };
  }

  const nav = navigator as Navigator & { gpu?: unknown; deviceMemory?: number };
  if (!nav.gpu) {
    return {
      state: 'unavailable',
      label: 'Unavailable',
      detail: 'This browser does not expose WebGPU.',
      forcedServer: true,
    };
  }

  const memoryGB = nav.deviceMemory;
  if (typeof memoryGB === 'number' && memoryGB < 4) {
    return {
      state: 'limited',
      label: 'Limited',
      detail: `${memoryGB} GB device memory detected; server mode is required.`,
      forcedServer: true,
    };
  }

  return {
    state: 'ready',
    label: 'Ready',
    detail: 'WebGPU is available for in-browser summaries.',
    forcedServer: false,
  };
}

export function getStoredModelId(): string {
  if (typeof window === 'undefined') return DEFAULT_MODEL_ID;
  const status = getWebGPUStatus();
  if (status.forcedServer) return 'server';
  return localStorage.getItem(IN_BROWSER_LLM_MODEL_KEY) || DEFAULT_MODEL_ID;
}

export function setStoredModelId(modelId: string): void {
  if (typeof window === 'undefined') return;
  localStorage.setItem(IN_BROWSER_LLM_MODEL_KEY, modelId);
}

export function getStoredDtype(modelId: string): Dtype | undefined {
  if (typeof window === 'undefined') return undefined;
  const raw = localStorage.getItem(IN_BROWSER_LLM_DTYPE_KEY);
  if (!raw) {
    // Fallback to entry's defaultDtype.
    const curated = CURATED_MODELS.find((m) => m.id === modelId);
    return curated?.defaultDtype;
  }
  try {
    const parsed = JSON.parse(raw) as Record<string, Dtype>;
    const stored = parsed[modelId];
    if (stored && ['q4', 'q4f16', 'q8', 'fp16'].includes(stored)) {
      return stored;
    }
  } catch { /* noop */ }
  const curated = CURATED_MODELS.find((m) => m.id === modelId);
  return curated?.defaultDtype;
}

export function setStoredDtype(modelId: string, dtype: Dtype): void {
  if (typeof window === 'undefined') return;
  const raw = localStorage.getItem(IN_BROWSER_LLM_DTYPE_KEY);
  let parsed: Record<string, Dtype> = {};
  if (raw) {
    try { parsed = JSON.parse(raw) as Record<string, Dtype>; } catch { /* noop */ }
  }
  parsed[modelId] = dtype;
  localStorage.setItem(IN_BROWSER_LLM_DTYPE_KEY, JSON.stringify(parsed));
}

export function getAvailableModels(): InBrowserModelOption[] {
  const records = llmRecords();
  const seen = new Set<string>();
  const options: InBrowserModelOption[] = [
    {
      id: 'server',
      actualModelId: null,
      label: 'Server-side Qwen 3.6',
      runtime: 'server',
      source: 'server',
    },
  ];

  for (const curated of CURATED_MODELS) {
    if (curated.runtime === 'web-llm') {
      const record = records.find((item) => item.model_id === curated.modelRef);
      // Records aren't loaded until loadWebLLM() runs at least once. If
      // we have no records yet (initial render before refresh), include
      // the curated entry so the picker has something to show.
      if (records.length > 0 && !record) continue;
      seen.add(curated.modelRef);
      options.push({
        id: curated.id,
        actualModelId: curated.modelRef,
        label: curated.label,
        runtime: 'web-llm',
        repoId: curated.modelRef,
        approxSizeMB: curated.approxSizeMB,
        contextWindow: curated.contextWindow,
        notes: curated.notes,
        vramRequiredMB: record?.vram_required_MB,
        lowResourceRequired: record?.low_resource_required,
        source: 'curated',
      });
    } else {
      // transformers.js entry — always shown (we don't precheck the HF
      // repo at picker render time).
      options.push({
        id: curated.id,
        actualModelId: curated.modelRef,
        label: curated.label,
        runtime: 'transformers.js',
        repoId: curated.modelRef,
        defaultDtype: curated.defaultDtype,
        approxSizeMB: curated.approxSizeMB,
        contextWindow: curated.contextWindow,
        notes: curated.notes,
        source: 'curated',
      });
    }
  }

  for (const record of records) {
    if (seen.has(record.model_id)) continue;
    if (!/(qwen|gemma|phi|llama|smollm|olmo|mistral)/i.test(record.model_id)) {
      continue;
    }
    options.push({
      id: record.model_id,
      actualModelId: record.model_id,
      label: displayName(record.model_id),
      runtime: 'web-llm',
      repoId: record.model_id,
      vramRequiredMB: record.vram_required_MB,
      lowResourceRequired: record.low_resource_required,
      source: 'web-llm',
    });
  }

  return options;
}

export async function refreshAvailableModels(): Promise<InBrowserModelOption[]> {
  await loadWebLLM();
  return getAvailableModels();
}

export function getModelLabel(modelId: string): string {
  if (!modelId) return 'Unknown';
  if (modelId === 'server') return 'Server Qwen 3.6';
  const option = getAvailableModels().find((item) => item.id === modelId);
  if (option) return option.label;
  return displayName(modelId);
}

// For web-llm entries, resolve the picker id → actual MLC model_id.
// transformers.js entries return their HF repo id; server returns 'server'.
export function resolveActualModelId(modelId: string): string {
  if (modelId === 'server') return 'server';
  const curated = CURATED_MODELS.find((m) => m.id === modelId);
  if (curated) return curated.modelRef;
  // Direct passthrough for web-llm record ids that weren't curated.
  if (findRecord(modelId)) return modelId;

  const defaultOption = CURATED_MODELS.find((m) => m.id === DEFAULT_MODEL_ID);
  if (defaultOption) return defaultOption.modelRef;

  const fallback = llmRecords().find((record) => /qwen|gemma/i.test(record.model_id));
  return fallback?.model_id || llmRecords()[0]?.model_id || modelId;
}

export function getRuntimeForModelId(modelId: string): ModelRuntime | 'server' {
  if (modelId === 'server') return 'server';
  const curated = CURATED_MODELS.find((m) => m.id === modelId);
  if (curated) return curated.runtime;
  // Unknown id — assume web-llm (prebuilt record passthrough).
  return 'web-llm';
}

function forceServer(modelId: string): boolean {
  return modelId === 'server' || getWebGPUStatus().forcedServer;
}

// ---- Slice system prompt (kept in sync with backend/api/recording.py)
// Structured grouping (Decisions / Action Items / Metrics / Risks) is
// the format Aaron validated against Qwen 3 0.6B in the browser: ~90%
// fact capture vs the prior narrative prompt. `/no_think` is appended
// here AND on the user message so Qwen 3 disables its internal thinking
// block at both ends; other models silently ignore the token.
export const SLICE_SYSTEM_PROMPT =
  'You are the meeting-notes writer. The transcript is the complete source: write the finished notes now. Never ask for a recording, another transcript, more context, or a summary, and never explain how to summarize. Use only transcript facts. Output concise bullets for a two-sentence Gist, Decisions, Action Items (owner and due date when stated; otherwise owner not stated), Metrics, and Risks or Open Questions. Write None noted for an empty section. /no_think';

// Final-summary roll-up at session end (local-only privacy mode). Same
// structural shape, but framed as a consolidation rather than a live tick.
export const FINAL_SUMMARY_SYSTEM_PROMPT =
  'You are the meeting-notes writer. Consolidate the supplied slice summaries into the finished meeting brief now. Never ask for a recording, another transcript, more context, or a summary, and never explain how to summarize. Use only supplied facts. Include a short Gist, Decisions, Action Items (owner and due date when stated; otherwise owner not stated), Metrics, and Risks or Open Questions. Write None noted for an empty section. /no_think';

const NO_THINK_TAG = '\n\n/no_think';

function buildUserPrompt(transcript: string, previousSummary?: string): string {
  const tail = transcript.slice(-50000);
  let body: string;
  if (previousSummary && previousSummary.trim()) {
    body = [
      'Previous running summary:',
      previousSummary.trim(),
      '',
      'New transcript since that summary (most recent at the bottom):',
      tail,
    ].join('\n');
  } else {
    body = [
      'Transcript so far (most recent at the bottom):',
      tail,
    ].join('\n');
  }
  return `${body}${NO_THINK_TAG}`;
}

function buildFinalSummaryUserPrompt(slicesText: string): string {
  return `Slice summaries to consolidate (oldest first):\n\n${slicesText}${NO_THINK_TAG}`;
}

// ---- Server SSE fallback (unchanged from previous slice landing)
async function* summarizeWithServer(
  transcript: string,
  opts?: SummarizeOptions,
): AsyncIterable<string> {
  const response = await fetch(`${config.apiUrl}/api/recordings/summarize-slice`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      transcript,
      previous_summary: opts?.previousSummary || undefined,
      session_id: opts?.sessionId || undefined,
    }),
    signal: opts?.signal,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || 'Server-side summary failed.');
  }
  if (!response.body) {
    throw new Error('Server stream returned an empty body.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      if (opts?.signal?.aborted) break;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop() ?? '';
      for (const event of events) {
        const line = event.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        const payload = line.slice(6).trim();
        if (!payload) continue;
        try {
          const parsed = JSON.parse(payload);
          if (parsed.token) {
            opts?.onToken?.(parsed.token);
            yield parsed.token as string;
          }
          if (parsed.error) {
            throw new Error(String(parsed.error));
          }
          if (parsed.done) {
            return;
          }
        } catch (err) {
          if (err instanceof Error && err.message) throw err;
        }
      }
    }
  } finally {
    try { reader.releaseLock(); } catch { /* noop */ }
  }
}

// =============================================================
// web-llm backend
// =============================================================

let webLLMEnginePromise: Promise<MLCEngine> | null = null;
let webLLMLoadedActualModelId: string | null = null;

async function loadWebLLMEngine(
  actualModelId: string,
  onProgress?: (report: InitProgressReport) => void,
): Promise<MLCEngine> {
  const webllm = await loadWebLLM();
  if (actualModelId === 'server') {
    throw new Error('Server mode does not load a browser model.');
  }

  const appConfig: AppConfig = {
    ...webllm.prebuiltAppConfig,
    cacheBackend: 'indexeddb',
  };

  if (!webLLMEnginePromise) {
    webLLMEnginePromise = webllm.CreateMLCEngine(actualModelId, {
      appConfig,
      initProgressCallback: onProgress,
      logLevel: 'WARN',
    });
    webLLMLoadedActualModelId = actualModelId;
    return webLLMEnginePromise;
  }

  const engine = await webLLMEnginePromise;
  engine.setInitProgressCallback(onProgress);
  if (webLLMLoadedActualModelId !== actualModelId) {
    await engine.reload(actualModelId);
    webLLMLoadedActualModelId = actualModelId;
  }
  return engine;
}

async function* summarizeWithWebLLM(
  transcript: string,
  actualModelId: string,
  opts?: SummarizeOptions,
): AsyncIterable<string> {
  const engine = await loadWebLLMEngine(actualModelId, opts?.onProgress);
  if (opts?.signal?.aborted) {
    await engine.interruptGenerate();
    return;
  }

  const abortHandler = () => {
    engine.interruptGenerate();
  };
  opts?.signal?.addEventListener('abort', abortHandler, { once: true });

  try {
    const stream = await engine.chat.completions.create({
      model: actualModelId,
      stream: true,
      temperature: 0.25,
      max_tokens: 600,
      messages: [
        { role: 'system', content: SLICE_SYSTEM_PROMPT },
        { role: 'user', content: buildUserPrompt(transcript, opts?.previousSummary) },
      ],
    } as any);

    for await (const chunk of stream as unknown as AsyncIterable<any>) {
      if (opts?.signal?.aborted) break;
      const token = chunk?.choices?.[0]?.delta?.content || '';
      if (!token) continue;
      opts?.onToken?.(token);
      yield token;
    }
  } finally {
    opts?.signal?.removeEventListener('abort', abortHandler);
  }
}

// =============================================================
// transformers.js backend
// =============================================================

let transformersModulePromise: Promise<any> | null = null;

async function loadTransformersModule(): Promise<any> {
  if (!transformersModulePromise) {
    // Dynamic import keeps the ~4MB transformers runtime out of the
    // initial bundle; only paid when the user actually picks a t.js model.
    transformersModulePromise = import('@huggingface/transformers');
  }
  return transformersModulePromise;
}

interface TransformersLoaded {
  repoId: string;
  dtype: Dtype;
  tokenizer: any;
  model: any;
  isMultimodal: boolean;
}

let transformersLoaded: TransformersLoaded | null = null;
let transformersLoadingPromise: Promise<TransformersLoaded> | null = null;

// Models known to expose vision/audio encoders on the same repo. Loaded
// via AutoModelForImageTextToText so the multimodal sessions are wired up
// correctly. We still drive them text-only.
const MULTIMODAL_REPO_PREFIXES = [
  'onnx-community/gemma-4',
  'onnx-community/Gemma-4',
];

function isMultimodalRepo(repoId: string): boolean {
  return MULTIMODAL_REPO_PREFIXES.some((p) => repoId.startsWith(p));
}

async function loadTransformersModel(
  repoId: string,
  dtype: Dtype,
  onProgress?: (report: InitProgressReport) => void,
): Promise<TransformersLoaded> {
  if (
    transformersLoaded &&
    transformersLoaded.repoId === repoId &&
    transformersLoaded.dtype === dtype
  ) {
    return transformersLoaded;
  }
  // If a different model was loaded, drop the reference so the GC can
  // reclaim the WebGPU buffers. (transformers.js doesn't expose an
  // explicit dispose; setting to null is the documented escape hatch.)
  if (transformersLoaded && transformersLoaded.repoId !== repoId) {
    transformersLoaded = null;
  }

  if (transformersLoadingPromise) return transformersLoadingPromise;

  const transformers = await loadTransformersModule();
  const { AutoTokenizer, AutoModelForCausalLM, AutoModelForImageTextToText } =
    transformers as {
      AutoTokenizer: any;
      AutoModelForCausalLM: any;
      AutoModelForImageTextToText: any;
    };

  const multimodal = isMultimodalRepo(repoId);

  // Progress shim: transformers.js emits a richer event shape. Translate
  // to the InitProgressReport shape callers already handle.
  const onTransformersProgress = onProgress
    ? (event: any) => {
        if (event?.status === 'progress' || event?.status === 'progress_total') {
          const progress =
            typeof event.progress === 'number' ? event.progress / 100 : 0;
          onProgress({
            progress: Math.max(0, Math.min(1, progress)),
            timeElapsed: 0,
            text: `Downloading ${event.file || repoId}`,
          });
        } else if (event?.status === 'ready') {
          onProgress({
            progress: 1,
            timeElapsed: 0,
            text: `${repoId} ready`,
          });
        }
      }
    : undefined;

  transformersLoadingPromise = (async () => {
    const tokenizer = await AutoTokenizer.from_pretrained(repoId, {
      progress_callback: onTransformersProgress,
    });

    let model: any;
    let usedMultimodal = false;
    if (multimodal) {
      // Try text-only path first: AutoModelForCausalLM against the
      // multimodal repo. If transformers.js can resolve a causal-LM
      // config from the repo's text submodule it skips downloading the
      // vision/audio encoder ONNX shards entirely (~270MB+ saved). If it
      // can't, we fall back to the original AutoModelForImageTextToText
      // loader. The first form is what we want — verify via Network
      // panel that vision_encoder_*/audio_encoder_* never request.
      try {
        model = await AutoModelForCausalLM.from_pretrained(repoId, {
          dtype,
          device: 'webgpu',
          progress_callback: onTransformersProgress,
        });
      } catch (causalErr) {
        // eslint-disable-next-line no-console
        console.warn(
          `[in-browser-llm] AutoModelForCausalLM rejected ${repoId}; ` +
            'falling back to AutoModelForImageTextToText (vision+audio sessions will load).',
          causalErr,
        );
        // Reference: webml-community/Gemma-4-WebGPU Space.
        // Per-submodule dtypes mirror that bundle's loader.
        model = await AutoModelForImageTextToText.from_pretrained(repoId, {
          dtype: {
            audio_encoder: 'fp16',
            vision_encoder: 'fp16',
            embed_tokens: dtype,
            decoder_model_merged: dtype,
          },
          device: 'webgpu',
          progress_callback: onTransformersProgress,
        });
        usedMultimodal = true;
      }
    } else {
      model = await AutoModelForCausalLM.from_pretrained(repoId, {
        dtype,
        device: 'webgpu',
        progress_callback: onTransformersProgress,
      });
    }

    const loaded: TransformersLoaded = {
      repoId,
      dtype,
      tokenizer,
      model,
      isMultimodal: usedMultimodal,
    };
    transformersLoaded = loaded;
    return loaded;
  })();

  try {
    return await transformersLoadingPromise;
  } finally {
    transformersLoadingPromise = null;
  }
}

interface TransformersGenerateOpts {
  signal?: AbortSignal;
  onToken?: (token: string) => void;
  onProgress?: (report: InitProgressReport) => void;
  systemPrompt: string;
  userContent: string;
  maxNewTokens?: number;
}

async function* generateWithTransformersJs(
  repoId: string,
  dtype: Dtype,
  opts: TransformersGenerateOpts,
): AsyncIterable<string> {
  const loaded = await loadTransformersModel(repoId, dtype, opts.onProgress);
  if (opts.signal?.aborted) return;

  const { tokenizer, model } = loaded;
  const transformers = await loadTransformersModule();
  const { TextStreamer } = transformers as { TextStreamer: any };

  const messages = [
    { role: 'system', content: opts.systemPrompt },
    { role: 'user', content: opts.userContent },
  ];

  // apply_chat_template returns token ids ready for generate().
  // enable_thinking: false disables Qwen 3's <think> block at the
  // template level. Other model templates ignore the flag.
  const inputs = tokenizer.apply_chat_template(messages, {
    add_generation_prompt: true,
    return_tensors: 'pt',
    return_dict: true,
    enable_thinking: false,
  });

  // Token streaming via TextStreamer.callback_function. We bridge that
  // callback into an AsyncIterable by buffering tokens and resolving a
  // pending pull when one lands. Typed loose to keep TS happy across
  // closure boundaries (TS narrows `pending` to never if it never sees
  // a typed write to it from the same flow graph).
  type TokenResolver = (v: IteratorResult<string>) => void;
  const state: {
    buffer: string[];
    pending: TokenResolver | null;
    done: boolean;
    error: Error | null;
  } = {
    buffer: [],
    pending: null,
    done: false,
    error: null,
  };

  const streamer = new TextStreamer(tokenizer, {
    skip_prompt: true,
    skip_special_tokens: true,
    callback_function: (text: string) => {
      if (!text) return;
      opts?.onToken?.(text);
      const resolver = state.pending;
      if (resolver) {
        state.pending = null;
        resolver({ value: text, done: false });
      } else {
        state.buffer.push(text);
      }
    },
  });

  // Abort wiring — transformers.js generate() honors an interrupt callback
  // via a stopping criterion. Simpler: track a flag and the streamer
  // callback will get further calls regardless; we early-return out of
  // the async iterator below.
  const abortHandler = () => {
    // Calling interrupt() short-circuits the model loop in 3.x+.
    if (typeof (model as any).interrupt === 'function') {
      try { (model as any).interrupt(); } catch { /* noop */ }
    }
  };
  opts?.signal?.addEventListener('abort', abortHandler, { once: true });

  // Kick off generation. We await this in a parallel task so the
  // AsyncIterable can yield buffered tokens as they land.
  const generatePromise = (async () => {
    try {
      const inputIds = (inputs && (inputs as any).input_ids) ?? inputs;
      await model.generate({
        ...((inputs && (inputs as any).input_ids) ? inputs : { input_ids: inputIds }),
        max_new_tokens: opts.maxNewTokens ?? 256,
        do_sample: true,
        temperature: 0.3,
        top_p: 0.9,
        streamer,
      });
    } catch (err) {
      state.error = err instanceof Error ? err : new Error(String(err));
    } finally {
      state.done = true;
      const resolver = state.pending;
      state.pending = null;
      if (resolver) {
        resolver({ value: undefined as any, done: true });
      }
    }
  })();

  try {
    while (true) {
      if (opts?.signal?.aborted) break;
      if (state.buffer.length > 0) {
        const next = state.buffer.shift()!;
        yield next;
        continue;
      }
      if (state.done) {
        if (state.error) throw state.error;
        break;
      }
      const value = await new Promise<IteratorResult<string>>((resolve) => {
        state.pending = resolve;
      });
      if (value.done) {
        if (state.error) throw state.error;
        break;
      }
      yield value.value;
    }
  } finally {
    opts?.signal?.removeEventListener('abort', abortHandler);
    await generatePromise.catch(() => undefined);
  }
}

// Slice-summary wrapper: applies the slice system prompt + user prompt
// + /no_think tail. Kept as a thin shim so the dispatcher reads naturally.
async function* summarizeWithTransformersJs(
  transcript: string,
  repoId: string,
  dtype: Dtype,
  opts?: SummarizeOptions,
): AsyncIterable<string> {
  yield* generateWithTransformersJs(repoId, dtype, {
    signal: opts?.signal,
    onToken: opts?.onToken,
    onProgress: opts?.onProgress,
    systemPrompt: SLICE_SYSTEM_PROMPT,
    userContent: buildUserPrompt(transcript, opts?.previousSummary),
    maxNewTokens: 256,
  });
}

// Final-summary roll-up: consolidates slice texts into one document.
// Larger max_new_tokens since this is the final pass and the user wants
// completeness over latency. Local-only privacy mode runs this at
// session-end; otherwise the server-side final-summary pipeline owns it.
async function* finalSummaryWithTransformersJs(
  slicesText: string,
  repoId: string,
  dtype: Dtype,
  opts?: SummarizeOptions,
): AsyncIterable<string> {
  yield* generateWithTransformersJs(repoId, dtype, {
    signal: opts?.signal,
    onToken: opts?.onToken,
    onProgress: opts?.onProgress,
    systemPrompt: FINAL_SUMMARY_SYSTEM_PROMPT,
    userContent: buildFinalSummaryUserPrompt(slicesText),
    maxNewTokens: 1024,
  });
}

// =============================================================
// Dispatcher
// =============================================================

export class InBrowserSummarizer implements Summarizer {
  async *summarize(
    transcript: string,
    modelId: string,
    opts?: SummarizeOptions,
  ): AsyncIterable<string> {
    if (forceServer(modelId)) {
      yield* summarizeWithServer(transcript, opts);
      return;
    }

    const runtime = getRuntimeForModelId(modelId);
    if (runtime === 'server') {
      yield* summarizeWithServer(transcript, opts);
      return;
    }

    if (runtime === 'transformers.js') {
      const curated = CURATED_MODELS.find((m) => m.id === modelId);
      if (!curated) {
        // Unknown id but tagged transformers.js — fall through to server.
        yield* summarizeWithServer(transcript, opts);
        return;
      }
      const dtype = getStoredDtype(modelId) || curated.defaultDtype || 'q4f16';
      yield* summarizeWithTransformersJs(transcript, curated.modelRef, dtype, opts);
      return;
    }

    // web-llm path
    const actualModelId = resolveActualModelId(modelId);
    yield* summarizeWithWebLLM(transcript, actualModelId, opts);
  }

  // Final-summary roll-up. Only the transformers.js + web-llm paths are
  // wired here; the local-only privacy mode never reaches server mode
  // (the caller gates on localOnly + WebGPU before invoking). If the
  // current model is server, the caller is expected to skip this and
  // surface "WebGPU required" instead.
  async *summarizeFinal(
    slicesText: string,
    modelId: string,
    opts?: SummarizeOptions,
  ): AsyncIterable<string> {
    if (forceServer(modelId)) {
      // Reuse the server slice endpoint as a fallback. Not ideal for
      // long roll-ups (the server prompt expects "running" framing) but
      // local-only mode never lands here.
      yield* summarizeWithServer(slicesText, opts);
      return;
    }
    const runtime = getRuntimeForModelId(modelId);
    if (runtime === 'transformers.js') {
      const curated = CURATED_MODELS.find((m) => m.id === modelId);
      if (!curated) {
        yield* summarizeWithServer(slicesText, opts);
        return;
      }
      const dtype = getStoredDtype(modelId) || curated.defaultDtype || 'q4f16';
      yield* finalSummaryWithTransformersJs(slicesText, curated.modelRef, dtype, opts);
      return;
    }
    // web-llm path: reuse the slice generator with a custom system
    // prompt by going through the SDK directly.
    const actualModelId = resolveActualModelId(modelId);
    const engine = await loadWebLLMEngine(actualModelId, opts?.onProgress);
    if (opts?.signal?.aborted) {
      await engine.interruptGenerate();
      return;
    }
    const abortHandler = () => { engine.interruptGenerate(); };
    opts?.signal?.addEventListener('abort', abortHandler, { once: true });
    try {
      const stream = await engine.chat.completions.create({
        model: actualModelId,
        stream: true,
        temperature: 0.25,
        max_tokens: 1024,
        messages: [
          { role: 'system', content: FINAL_SUMMARY_SYSTEM_PROMPT },
          { role: 'user', content: buildFinalSummaryUserPrompt(slicesText) },
        ],
      } as any);
      for await (const chunk of stream as unknown as AsyncIterable<any>) {
        if (opts?.signal?.aborted) break;
        const token = chunk?.choices?.[0]?.delta?.content || '';
        if (!token) continue;
        opts?.onToken?.(token);
        yield token;
      }
    } finally {
      opts?.signal?.removeEventListener('abort', abortHandler);
    }
  }

  async preloadModel(modelId: string, opts?: PreloadOptions): Promise<void> {
    if (forceServer(modelId)) return;
    const runtime = getRuntimeForModelId(modelId);
    if (runtime === 'server') return;

    if (runtime === 'transformers.js') {
      const curated = CURATED_MODELS.find((m) => m.id === modelId);
      if (!curated) return;
      const dtype = opts?.dtype || getStoredDtype(modelId) || curated.defaultDtype || 'q4f16';
      await loadTransformersModel(curated.modelRef, dtype, opts?.onProgress);
      return;
    }

    const actualModelId = resolveActualModelId(modelId);
    const engine = await loadWebLLMEngine(actualModelId, opts?.onProgress);
    if (opts?.signal?.aborted) {
      await engine.interruptGenerate();
    }
  }
}

// Backwards-compat alias for any consumer that still imports the old name.
export class WebLLMSummarizer extends InBrowserSummarizer {}

export const inBrowserLLM = new InBrowserSummarizer();
