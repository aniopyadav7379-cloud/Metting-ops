import { useEffect, useMemo, useState } from 'react';
import { Brain, Download, Lock, Mic, RefreshCw } from 'lucide-react';
import {
  DEFAULT_AUTO_CONFIG,
  DEFAULT_MODEL_ID,
  LIVE_SUMMARY_THRESHOLDS,
  getAvailableModels,
  getLiveSummaryAutoConfig,
  getStoredDtype,
  getStoredModelId,
  getWebGPUStatus,
  inBrowserLLM,
  refreshAvailableModels,
  setLiveSummaryAutoConfig,
  setStoredDtype,
  setStoredModelId,
  type Dtype,
  type InBrowserModelOption,
  type LiveSummaryThreshold,
} from '../../services/inBrowserLLM';
import {
  inBrowserSTT,
} from '../../services/inBrowserSTT';
import {
  getSTTCapability,
  getSTTMode,
  setSTTMode,
  type SttMode,
} from '../../services/inBrowserSTTSettings';
import {
  getLocalOnly,
  isPrivacyModeAvailable,
  setLocalOnly,
} from '../../services/privacyMode';
import type { SectionProps } from './SettingsContext';

const QWEN3_MODEL_IDS = new Set<string>([
  'qwen3-0.6b-onnx-q4f16',
  'qwen3.6-1.7b',
  'qwen3-1.7b',
  'qwen3-0.6b',
]);

function isQwen3Model(modelId: string): boolean {
  return QWEN3_MODEL_IDS.has(modelId);
}

function isGemma4Model(modelId: string): boolean {
  return modelId === 'gemma-4-e2b-q4f16' || modelId === 'gemma-4-e2b-it';
}

function statusClasses(state: 'ready' | 'limited' | 'unavailable'): string {
  if (state === 'ready') return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
  if (state === 'limited') return 'border-yellow-500/30 bg-yellow-500/10 text-yellow-300';
  return 'border-red-500/30 bg-red-500/10 text-red-300';
}

function runtimeBadgeClasses(runtime: InBrowserModelOption['runtime']): string {
  if (runtime === 'transformers.js') return 'border-violet-500/40 bg-violet-500/10 text-violet-200';
  if (runtime === 'web-llm') return 'border-sky-500/40 bg-sky-500/10 text-sky-200';
  return 'border-zinc-600 bg-zinc-700/40 text-zinc-300';
}

function formatSize(mb?: number): string | null {
  if (!mb || mb <= 0) return null;
  if (mb >= 1024) return `~${(mb / 1024).toFixed(1)}GB`;
  return `~${mb}MB`;
}

function optionDetail(option?: InBrowserModelOption): string {
  if (!option) return '';
  if (option.id === 'server') return 'Highest quality, uses the backend Qwen 3.6 endpoint.';
  const parts: string[] = [];
  if (option.runtime === 'web-llm' && option.vramRequiredMB) {
    parts.push(`${(option.vramRequiredMB / 1024).toFixed(1)} GB VRAM`);
  }
  const size = formatSize(option.approxSizeMB);
  if (size) parts.push(size);
  if (option.contextWindow) parts.push(`${option.contextWindow.toLocaleString()} ctx`);
  if (option.lowResourceRequired) parts.push('low-resource profile');
  if (option.notes) parts.push(option.notes);
  parts.push(option.source === 'curated' ? 'curated default' : 'web-llm prebuilt');
  return parts.join(' · ');
}

const DTYPE_OPTIONS: Array<{ value: Dtype; label: string }> = [
  { value: 'q4f16', label: 'q4f16 (smallest, fastest)' },
  { value: 'q4', label: 'q4 (small, slower)' },
  { value: 'q8', label: 'q8 (medium)' },
  { value: 'fp16', label: 'fp16 (largest, highest quality)' },
];

export default function InBrowserAISettings(_props: SectionProps) {
  const [models, setModels] = useState<InBrowserModelOption[]>([]);
  const [selectedModel, setSelectedModel] = useState(getStoredModelId);
  const [serverMode, setServerMode] = useState(() => getStoredModelId() === 'server');
  const [selectedDtype, setSelectedDtype] = useState<Dtype | undefined>(() =>
    getStoredDtype(getStoredModelId()),
  );
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null);
  const [downloadState, setDownloadState] = useState<'idle' | 'downloading' | 'ready' | 'error'>('idle');
  const [autoConfig, setAutoConfig] = useState(() => getLiveSummaryAutoConfig());
  // Phase B.3 polish: server-side summarizer label fetched from
  // /api/system/pipeline. Surfaces which model actually drives the live
  // progressive summaries during normal-mode recordings, so users do not
  // mistake the in-browser dropdown above for a global setting.
  const [serverSideSummarizerLabel, setServerSideSummarizerLabel] = useState<string>('');
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/system/pipeline');
        if (cancelled || !res.ok) return;
        const data = await res.json();
        const models = Array.isArray(data?.available_llm_models) ? data.available_llm_models : [];
        const def = models.find((m: { default?: boolean }) => m.default);
        const label = def?.label || data?.llm?.label || data?.llm?.model || '';
        if (label) setServerSideSummarizerLabel(String(label));
      } catch {
        // leave blank — paragraph falls back to a generic message
      }
    })();
    return () => { cancelled = true; };
  }, []);
  // Privacy mode: persisted in localStorage. Forces browser STT + local
  // LLM, never writes to the server, persists session data in
  // IndexedDB. Available iff WebGPU exists (the final-summary roll-up
  // needs it). The toggle here is the source of truth.
  const [localOnly, setLocalOnlyState] = useState<boolean>(() => getLocalOnly());
  const localOnlyAvailable = useMemo(() => isPrivacyModeAvailable(), []);
  // Speech-to-Text: own state, separate from the LLM picker. Default ON
  // when the browser can host the model; falls back automatically when
  // it can't.
  const [sttMode, setSttModeLocal] = useState<SttMode>(() => getSTTMode());
  const sttCapability = useMemo(() => getSTTCapability(), []);
  const [sttReady, setSttReady] = useState<boolean>(() => inBrowserSTT.isReady());
  const [sttLastRTF, setSttLastRTF] = useState<number | null>(() =>
    inBrowserSTT.getDiagnostics().lastRTF,
  );
  // STT pre-download: optional manual trigger so users can warm the
  // CacheStorage entry before their first meeting (rather than eating
  // a ~10-20s download mid-meeting). Pushes the same load() the always-on
  // session calls — no transcribe.
  const [sttPredownloadState, setSttPredownloadState] =
    useState<'idle' | 'downloading' | 'ready' | 'error'>(() =>
      inBrowserSTT.isReady() ? 'ready' : 'idle',
    );
  const [sttPredownloadProgress, setSttPredownloadProgress] = useState<number | null>(null);
  const [sttPredownloadLabel, setSttPredownloadLabel] = useState<string | null>(null);

  const webGPUStatus = useMemo(() => getWebGPUStatus(), []);
  const selectedOption = models.find((model) => model.id === selectedModel);
  const showDtypeSelector = selectedOption?.runtime === 'transformers.js';

  useEffect(() => {
    const available = getAvailableModels();
    setModels(available);
    const current = getStoredModelId();
    if (!available.some((model) => model.id === current)) {
      const fallback = available.find((model) => model.id !== 'server')?.id || 'server';
      setSelectedModel(fallback);
      setStoredModelId(fallback);
    }
    refreshAvailableModels().then((loaded) => {
      setModels(loaded);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (webGPUStatus.forcedServer && selectedModel !== 'server') {
      setSelectedModel('server');
      setServerMode(true);
      setStoredModelId('server');
    }
  }, [selectedModel, webGPUStatus.forcedServer]);

  // When the model changes, prefer that model's persisted dtype (or its
  // default). This keeps each model's quantization choice independent.
  useEffect(() => {
    setSelectedDtype(getStoredDtype(selectedModel));
  }, [selectedModel]);

  const updateModel = (modelId: string) => {
    setSelectedModel(modelId);
    setServerMode(modelId === 'server');
    setStoredModelId(modelId);
    // Reset cached download UI state when switching models.
    setDownloadProgress(null);
    setDownloadState('idle');
  };

  const updateDtype = (dtype: Dtype) => {
    setSelectedDtype(dtype);
    setStoredDtype(selectedModel, dtype);
  };

  const updateServerMode = (enabled: boolean) => {
    setServerMode(enabled);
    if (enabled) {
      updateModel('server');
      return;
    }
    const fallback = models.find((model) => model.id !== 'server')?.id || 'server';
    updateModel(fallback);
  };

  // Poll the STT singleton because the model can load in the background
  // (the AlwaysOnContext kicks the load). We re-read each tick; the
  // singleton itself is the source of truth.
  useEffect(() => {
    const tick = window.setInterval(() => {
      setSttReady(inBrowserSTT.isReady());
      setSttLastRTF(inBrowserSTT.getDiagnostics().lastRTF);
    }, 1000);
    return () => window.clearInterval(tick);
  }, []);

  const updateSttMode = (next: SttMode) => {
    setSttModeLocal(next);
    setSTTMode(next);
  };

  const updateLocalOnly = (next: boolean) => {
    setLocalOnlyState(next);
    setLocalOnly(next);
    // When the user opts in, force browser STT + non-server LLM to
    // match. Standard mode preserves the previous picks.
    if (next) {
      if (sttMode !== 'browser-parakeet') updateSttMode('browser-parakeet');
      if (serverMode || selectedModel === 'server') {
        const fallback = models.find((model) => model.id !== 'server')?.id || DEFAULT_MODEL_ID;
        updateModel(fallback);
      }
    }
  };

  const updateAutoEnabled = (enabled: boolean) => {
    const next = { ...autoConfig, enabled };
    setAutoConfig(next);
    setLiveSummaryAutoConfig(next);
  };

  const updateAutoThreshold = (threshold: LiveSummaryThreshold) => {
    const next = { ...autoConfig, threshold };
    setAutoConfig(next);
    setLiveSummaryAutoConfig(next);
  };

  const downloadModel = async () => {
    if (selectedModel === 'server') return;
    setDownloadState('downloading');
    setDownloadProgress(0);
    try {
      await inBrowserLLM.preloadModel(selectedModel, {
        dtype: selectedDtype,
        onProgress: (report) => {
          setDownloadProgress(Math.max(0, Math.min(1, report.progress || 0)));
        },
      });
      setDownloadProgress(1);
      setDownloadState('ready');
    } catch {
      setDownloadState('error');
    }
  };

  // Warm the STT model cache without starting a meeting. Calls into the
  // singleton's load() — idempotent, dedupes with any concurrent loader
  // (e.g. always-on kicking the same load on first session start).
  const predownloadSttModel = async () => {
    if (!sttCapability.available || sttMode !== 'browser-parakeet') return;
    setSttPredownloadState('downloading');
    setSttPredownloadProgress(0);
    setSttPredownloadLabel('Starting...');
    try {
      await inBrowserSTT.load({
        onProgress: (progress, label) => {
          setSttPredownloadProgress(Math.max(0, Math.min(1, progress)));
          if (label) setSttPredownloadLabel(label);
        },
      });
      setSttPredownloadProgress(1);
      setSttPredownloadState('ready');
      setSttPredownloadLabel('Cached');
    } catch (err) {
      console.error('[InBrowserAISettings] STT preload failed', err);
      setSttPredownloadState('error');
      setSttPredownloadLabel(err instanceof Error ? err.message : 'Download failed');
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-white">In-browser AI</h3>
          <p className="mt-1 text-sm text-zinc-400">
            Browser summaries are private and fast after download. Server mode is stronger and uses backend GPU time.
          </p>
        </div>
        <span className={`rounded-full border px-3 py-1 text-xs font-medium ${statusClasses(webGPUStatus.state)}`}>
          WebGPU {webGPUStatus.label}
        </span>
      </div>

      <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4 space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h4 className="flex items-center gap-2 text-sm font-medium text-white">
              <Lock className="h-4 w-4 text-emerald-300" /> Privacy Mode
            </h4>
            <p className="mt-1 text-xs text-zinc-400">
              Recommended for sensitive meetings. Transcript, summaries, and final notes stay in your browser's storage.
              Trade-off: no cross-meeting search, no shared access from other devices, no server-side high-accuracy reprocess.
            </p>
          </div>
        </div>
        <label className="flex items-center justify-between gap-4 rounded-md border border-zinc-800 bg-black/40 px-3 py-2">
          <span>
            <span className="block text-sm font-medium text-zinc-200">
              Local-only (nothing leaves this device)
            </span>
            <span className="mt-0.5 block text-xs text-zinc-500">
              {localOnlyAvailable
                ? 'Forces browser STT + in-browser LLM. Sessions are saved only in this browser.'
                : 'WebGPU is required for the local final-summary roll-up. Toggle disabled on this device.'}
            </span>
          </span>
          <input
            type="checkbox"
            checked={localOnly && localOnlyAvailable}
            disabled={!localOnlyAvailable}
            onChange={(event) => updateLocalOnly(event.target.checked)}
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
          />
        </label>
        {localOnly && !localOnlyAvailable && (
          <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
            WebGPU required for local-only mode. Falling back to standard mode for this session.
          </p>
        )}
      </div>

      <div className="rounded-lg border border-zinc-800 bg-black/20 p-4 space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h4 className="flex items-center gap-2 text-sm font-medium text-zinc-200">
              <Mic className="h-4 w-4 text-zinc-400" /> Speech-to-Text
            </h4>
            <p className="mt-1 text-xs text-zinc-500">
              Run STT in the browser with Parakeet 0.6B INT8 (~880 MB). Downloaded from HuggingFace on first session, cached in browser thereafter. Falls back to the server when the browser can't host the model.
            </p>
          </div>
          <span
            className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
              sttCapability.available
                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                : 'border-yellow-500/30 bg-yellow-500/10 text-yellow-300'
            }`}
          >
            {sttCapability.available ? 'Browser capable' : 'Server only'}
          </span>
        </div>

        <label className="flex items-center justify-between gap-4 rounded-md border border-zinc-800 bg-black/40 px-3 py-2">
          <span>
            <span className="block text-sm font-medium text-zinc-200">Run STT in browser (Parakeet 0.6B INT8)</span>
            <span className="mt-0.5 block text-xs text-zinc-500">
              {sttCapability.available
                ? localOnly
                  ? 'Locked ON by Privacy Mode. Audio never leaves the device.'
                  : 'Recommended. Audio never leaves the device.'
                : sttCapability.reason || 'Browser cannot host the STT model.'}
            </span>
          </span>
          <input
            type="checkbox"
            checked={sttMode === 'browser-parakeet'}
            disabled={!sttCapability.available || localOnly}
            onChange={(event) => updateSttMode(event.target.checked ? 'browser-parakeet' : 'server-parakeet-1.1b')}
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
          />
        </label>

        <div className="rounded-md border border-zinc-800 bg-black/40 px-3 py-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-zinc-300">Server STT (Parakeet 1.1B fp16)</span>
            <span className="rounded-full border border-zinc-600 bg-zinc-700/40 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-300">
              Fallback / high accuracy
            </span>
          </div>
          <p className="mt-1 text-xs text-zinc-500">
            Used automatically when the browser model is unavailable, still loading, or has failed mid-meeting. Post-meeting reprocess still happens server-side.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
          <div className="rounded-md border border-zinc-800 bg-black/40 px-3 py-2">
            <div className="text-zinc-500">Model status</div>
            <div className="mt-0.5 text-zinc-200">
              {sttMode !== 'browser-parakeet'
                ? 'Disabled (server mode)'
                : sttReady
                  ? 'Cached / ready'
                  : 'Not loaded yet — loads on next meeting'}
            </div>
          </div>
          <div className="rounded-md border border-zinc-800 bg-black/40 px-3 py-2">
            <div className="text-zinc-500">Last observed RTF</div>
            <div className="mt-0.5 text-zinc-200">
              {sttLastRTF === null
                ? 'No measurement yet'
                : `${sttLastRTF.toFixed(3)}x (${sttLastRTF < 1 ? 'faster than realtime' : 'slower than realtime'})`}
            </div>
          </div>
        </div>

        <div className="pt-1">
          <button
            type="button"
            onClick={predownloadSttModel}
            disabled={
              !sttCapability.available ||
              sttMode !== 'browser-parakeet' ||
              sttPredownloadState === 'downloading' ||
              (sttPredownloadState === 'ready' && sttReady)
            }
            className="inline-flex min-h-9 items-center gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-200 transition hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {sttPredownloadState === 'downloading' ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5" />
            )}
            {sttReady ? 'STT model cached' : 'Pre-download STT model'}
          </button>
          {sttPredownloadProgress !== null && (
            <div className="mt-2 max-w-md">
              <div className="mb-1 flex items-center justify-between text-[10px] text-zinc-500">
                <span>{sttPredownloadLabel || 'Downloading'}</span>
                <span>{Math.round(sttPredownloadProgress * 100)}%</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded bg-zinc-800">
                <div
                  className={`h-full transition-all ${
                    sttPredownloadState === 'error' ? 'bg-red-400' : 'bg-emerald-400'
                  }`}
                  style={{ width: `${Math.round(sttPredownloadProgress * 100)}%` }}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      <div>
        <label className="mb-2 block text-sm font-medium text-zinc-300" htmlFor="in-browser-model">
          In-browser summary model
          <span className="ml-2 text-xs font-normal text-zinc-500">
            (privacy mode + browser-only fallback)
          </span>
        </label>
        <p className="mb-2 text-xs text-zinc-500">
          This model runs <em>only</em> inside your browser. Used when Privacy Mode is on,
          or as a fallback when the server-side path isn't reachable. Normal-mode recordings
          send audio to the server and the server-side summarizer (shown below) produces the
          live summary you see during recording.
        </p>
        <select
          id="in-browser-model"
          value={selectedModel}
          onChange={(event) => updateModel(event.target.value)}
          disabled={webGPUStatus.forcedServer}
          className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-zinc-200 outline-none focus:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {models.map((model) => {
            const size = formatSize(model.approxSizeMB);
            const suffix = size ? ` — ${size}` : '';
            const isDefault = model.id === DEFAULT_MODEL_ID;
            const tag = isDefault
              ? ' [DEFAULT]'
              : isGemma4Model(model.id)
                ? ' [PREMIUM QUALITY]'
                : '';
            return (
              <option key={model.id} value={model.id}>
                {model.label}
                {suffix}
                {tag}
              </option>
            );
          })}
        </select>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {selectedOption && (
            <span
              className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${runtimeBadgeClasses(selectedOption.runtime)}`}
            >
              {selectedOption.runtime}
            </span>
          )}
          {selectedOption?.id === DEFAULT_MODEL_ID && (
            <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-emerald-200">
              Recommended default
            </span>
          )}
          {selectedOption && isGemma4Model(selectedOption.id) && (
            <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-200">
              Premium quality · ~3GB first download
            </span>
          )}
          {selectedOption && isQwen3Model(selectedOption.id) && (
            <span className="rounded-full border border-violet-500/40 bg-violet-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-violet-200">
              Thinking mode: off
            </span>
          )}
          <p className="text-xs text-zinc-500">
            {webGPUStatus.forcedServer ? webGPUStatus.detail : optionDetail(selectedOption)}
          </p>
        </div>
      </div>

      {/* Phase B.3 polish: surface the server-side summarizer model so users
          understand which model actually produces the live summary they see
          during recording. Read-only display fetched from /api/system/pipeline;
          per-org overrides (admin only) live in Settings > AI Providers. */}
      <div className="rounded-lg border border-zinc-800 bg-black/20 p-4 space-y-1">
        <h4 className="flex items-center gap-2 text-sm font-medium text-zinc-200">
          <Brain className="h-4 w-4 text-pink-400" /> Server-side summarizer
          <span className="text-xs font-normal text-zinc-500">(used for normal-mode recordings)</span>
        </h4>
        <p className="text-xs text-zinc-500">
          {serverSideSummarizerLabel
            ? <>Currently <span className="text-zinc-300 font-medium">{serverSideSummarizerLabel}</span> on backend GPU. Live progressive summaries during recording use this model regardless of the in-browser selection above.</>
            : <>Configured by your organization; the backend default is set via <code className="text-zinc-400">MEETING_OPS_LLM_MODEL</code> env. Live progressive summaries during recording use this model regardless of the in-browser selection above.</>
          }
        </p>
      </div>

      {showDtypeSelector && (
        <div>
          <label className="mb-2 block text-sm font-medium text-zinc-300" htmlFor="in-browser-dtype">
            Quantization (dtype)
          </label>
          <select
            id="in-browser-dtype"
            value={selectedDtype || selectedOption?.defaultDtype || 'q4f16'}
            onChange={(event) => updateDtype(event.target.value as Dtype)}
            disabled={webGPUStatus.forcedServer}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-zinc-200 outline-none focus:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {DTYPE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-zinc-500">
            Lower precision = smaller download + faster, at some quality cost. transformers.js only.
          </p>
        </div>
      )}

      <label className="flex items-center justify-between gap-4 rounded-lg border border-zinc-800 bg-black/20 px-4 py-3">
        <span>
          <span className="block text-sm font-medium text-zinc-200">Use server-side LLM instead</span>
          <span className="mt-1 block text-xs text-zinc-500">
            {localOnly
              ? 'Locked OFF by Privacy Mode — summaries stay on this device.'
              : 'Routes summaries to backend Qwen 3.6.'}
          </span>
        </span>
        <input
          type="checkbox"
          checked={serverMode && !localOnly}
          disabled={localOnly}
          onChange={(event) => updateServerMode(event.target.checked)}
          className="h-4 w-4 rounded border-zinc-600 bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
        />
      </label>

      <div className="rounded-lg border border-zinc-800 bg-black/20 p-4 space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h4 className="text-sm font-medium text-zinc-200">Auto-summarize during always-on</h4>
            <p className="mt-1 text-xs text-zinc-500">
              Granola-style live notes — a new summary slice appears each time the threshold is reached.
            </p>
          </div>
          <input
            type="checkbox"
            checked={autoConfig.enabled}
            onChange={(event) => updateAutoEnabled(event.target.checked)}
            className="mt-1 h-4 w-4 rounded border-zinc-600 bg-zinc-800"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500" htmlFor="auto-summary-threshold">
            Threshold
          </label>
          <select
            id="auto-summary-threshold"
            value={autoConfig.threshold}
            onChange={(event) => updateAutoThreshold(event.target.value as LiveSummaryThreshold)}
            disabled={!autoConfig.enabled}
            className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-sm text-zinc-200 outline-none focus:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {LIVE_SUMMARY_THRESHOLDS.map((threshold) => (
              <option key={threshold.id} value={threshold.id}>
                {threshold.label}
              </option>
            ))}
          </select>
          <p className="mt-2 text-xs text-zinc-500">
            {autoConfig.enabled
              ? 'Manual "Summarize now" also still works and appends to the slice stack.'
              : `Disabled. Default is ${DEFAULT_AUTO_CONFIG.threshold.replace('_', ' ')}.`}
          </p>
        </div>
      </div>

      <div>
        <button
          onClick={downloadModel}
          disabled={serverMode || webGPUStatus.forcedServer || downloadState === 'downloading'}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-sky-500/40 bg-sky-500/10 px-4 py-2.5 text-sm font-medium text-sky-200 transition hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {downloadState === 'downloading' ? (
            <RefreshCw className="h-4 w-4 animate-spin" />
          ) : (
            <Download className="h-4 w-4" />
          )}
          Download model now
        </button>

        {downloadProgress !== null && (
          <div className="mt-3 max-w-md">
            <div className="mb-1 flex items-center justify-between text-xs text-zinc-500">
              <span>{downloadState === 'ready' ? 'Cached' : downloadState === 'error' ? 'Download failed' : 'Downloading'}</span>
              <span>{Math.round(downloadProgress * 100)}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-zinc-800">
              <div
                className={`h-full transition-all ${downloadState === 'error' ? 'bg-red-400' : 'bg-sky-400'}`}
                style={{ width: `${Math.round(downloadProgress * 100)}%` }}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
