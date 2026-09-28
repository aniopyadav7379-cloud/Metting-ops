import React, { useEffect, useState } from 'react';
import { Cpu, Users, Sparkles, FileText, ChevronDown, ChevronRight, Brain } from 'lucide-react';

// Static fallback used while /api/system/pipeline is in flight or unreachable.
// The live list always wins when it loads. Kept short on purpose — the real
// catalog comes from the backend.
const FALLBACK_LLM_MODELS: { id: string; label: string; default?: boolean }[] = [
  { id: '', label: 'Use backend default' },
];

interface AvailableLlmModel {
  id: string;
  label: string;
  route: 'direct' | 'litellm';
  ready: boolean;
  default: boolean;
}

// Module-level cache so /api/system/pipeline is fetched at most once per
// page load no matter how many Panel instances mount (Re-process modal,
// SessionCreator inline, etc.). Cleared on a full SPA reload.
const pipelineCache = (() => {
  let value: { models: AvailableLlmModel[]; defaultLabel: string } | null = null;
  return {
    read: () => value,
    write: (v: { models: AvailableLlmModel[]; defaultLabel: string }) => {
      value = v;
    },
  };
})();

export interface TranscriptionOptions {
  stt_provider?: 'whisper' | 'parakeet' | null;
  stt_model?: string | null;
  language?: string | null;
  diarization_mode?: 'auto' | 'manual' | 'off' | null;
  diarization_num_speakers?: number | null;
  // Lower threshold => more speakers (aggressive splitting); higher => fewer
  // (conservative merging). Stock pyannote default is ~0.7045; we ship 0.72.
  // UI presents this as a 3-tier picker (Loose / Balanced / Strict).
  diarization_clustering_threshold?: number | null;
  voice_fingerprint?: boolean | null;
  summary_template?: 'standard' | 'executive' | 'technical' | null;
  // LLM override for summarization. Empty = org default
  // (LLM_MODEL_QUALITY env -> gemma-4-26b-moe today).
  llm_model?: string | null;
  // When false (default), suppresses <think> blocks for Qwen 3.5/3.6 and
  // Gemma 4 chat templates. Flip on for reasoning-heavy work.
  llm_thinking?: boolean | null;
}

export const DEFAULT_TRANSCRIPTION_OPTIONS: TranscriptionOptions = {
  stt_provider: null,
  diarization_mode: null,
  voice_fingerprint: null,
  summary_template: null,
};

interface Props {
  value: TranscriptionOptions;
  onChange: (next: TranscriptionOptions) => void;
  /** Default expanded? Useful in modals where space allows. */
  defaultOpen?: boolean;
  /** Hide org-level note (used when caller already explained the override semantics). */
  hideHelp?: boolean;
}

export const TranscriptionOptionsPanel: React.FC<Props> = ({
  value,
  onChange,
  defaultOpen = false,
  hideHelp = false,
}) => {
  const [open, setOpen] = useState(defaultOpen);
  const [llmModels, setLlmModels] = useState<AvailableLlmModel[]>([]);
  const [defaultLlmLabel, setDefaultLlmLabel] = useState<string>('');

  // Live model catalog from the backend — replaces the old hardcoded
  // LOCAL_LLM_MODELS array so the dropdown always shows what is actually
  // loaded on our GPUs (currently Qwen3.6-35B-A3B-Vision on midboy1 P40,
  // plus the LiteLLM local catalog).
  useEffect(() => {
    let cancelled = false;
    // Module-level cache so multiple Panel instances on the same page
    // (Re-process modal + SessionCreator + SessionDetails) share one
    // request. The cache persists for the lifetime of the SPA load so
    // refresh-on-reopen is intentional via component remount, not via
    // polling.
    const cached = pipelineCache.read();
    if (cached) {
      setLlmModels(cached.models);
      setDefaultLlmLabel(cached.defaultLabel);
      return;
    }
    (async () => {
      try {
        const res = await fetch('/api/system/pipeline');
        if (cancelled) return;
        // Auth gone — leave the fallback in place and stop. AuthContext
        // handles the re-login prompt elsewhere; we shouldn't spam more
        // 401s from the picker.
        if (res.status === 401 || res.status === 403) return;
        if (!res.ok) return;
        const data = await res.json();
        const models: AvailableLlmModel[] = Array.isArray(data?.available_llm_models)
          ? data.available_llm_models
          : [];
        const def = models.find((m) => m.default);
        const defaultLabel = def?.label || data?.llm?.label || data?.llm?.model || '';
        pipelineCache.write({ models, defaultLabel });
        setLlmModels(models);
        setDefaultLlmLabel(defaultLabel);
      } catch {
        /* leave fallback */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const set = <K extends keyof TranscriptionOptions>(k: K, v: TranscriptionOptions[K]) =>
    onChange({ ...value, [k]: v });

  const provider = value.stt_provider ?? '';
  const diarMode = value.diarization_mode ?? 'auto';
  const summaryTpl = value.summary_template ?? 'standard';
  const voiceFP = value.voice_fingerprint !== false; // default on

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left"
      >
        <span className="flex items-center gap-2 text-sm font-medium text-gray-200">
          <Sparkles className="h-4 w-4 text-purple-400" />
          Transcription options
          <span className="text-xs text-gray-500 font-normal">
            (optional, overrides org defaults)
          </span>
        </span>
        {open ? (
          <ChevronDown className="h-4 w-4 text-gray-500" />
        ) : (
          <ChevronRight className="h-4 w-4 text-gray-500" />
        )}
      </button>
      {open && (
        <div className="space-y-4 border-t border-gray-800 px-4 py-4">
          {/* STT engine */}
          <div>
            <label className="mb-1 flex items-center gap-2 text-xs font-medium text-gray-300">
              <Cpu className="h-3.5 w-3.5 text-cyan-400" />
              Speech-to-text engine
            </label>
            <select
              value={provider}
              onChange={(e) => set('stt_provider', (e.target.value || null) as any)}
              className="w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white focus:border-cyan-500 focus:outline-none"
            >
              <option value="">Use org default</option>
              <option value="parakeet">NeMo Parakeet-TDT 1.1B (fast, ~30× realtime, word timestamps)</option>
              <option value="whisper">whisper.cpp large-v3-turbo (legacy, bring-your-own endpoint)</option>
            </select>
          </div>

          {/* Diarization */}
          <div>
            <label className="mb-1 flex items-center gap-2 text-xs font-medium text-gray-300">
              <Users className="h-3.5 w-3.5 text-emerald-400" />
              Speaker diarization
            </label>
            <div className="grid grid-cols-3 gap-2">
              {(['auto', 'manual', 'off'] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => set('diarization_mode', mode)}
                  className={`rounded border px-3 py-2 text-xs ${
                    diarMode === mode
                      ? 'border-emerald-500 bg-emerald-500/10 text-emerald-200'
                      : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                  }`}
                >
                  {mode === 'auto'
                    ? 'Auto-detect'
                    : mode === 'manual'
                    ? 'Manual count'
                    : 'No diarization'}
                </button>
              ))}
            </div>
            {diarMode === 'manual' && (
              <div className="mt-2 flex items-center gap-3">
                <label className="text-xs text-gray-400">Speakers:</label>
                <input
                  type="number"
                  min={1}
                  max={24}
                  value={value.diarization_num_speakers ?? 2}
                  onChange={(e) =>
                    set('diarization_num_speakers', Math.max(1, Math.min(24, Number(e.target.value) || 1)))
                  }
                  className="w-20 rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-white"
                />
              </div>
            )}
            {diarMode === 'auto' && !hideHelp && (
              <p className="mt-2 text-[11px] text-gray-500">
                Speaker sensitivity is tuned centrally for reliable GPU
                processing. Use Manual count only when you know exactly how
                many people spoke.
              </p>
            )}
          </div>

          {/* Voice fingerprinting */}
          <div>
            <label className="flex items-center gap-2 text-xs font-medium text-gray-300">
              <input
                type="checkbox"
                checked={voiceFP}
                onChange={(e) => set('voice_fingerprint', e.target.checked)}
                className="h-3.5 w-3.5 rounded border-gray-600 bg-gray-800 text-purple-500"
                disabled={diarMode === 'off'}
              />
              <span className={diarMode === 'off' ? 'text-gray-500' : ''}>
                Match speakers against the org voice library
              </span>
            </label>
            {!hideHelp && (
              <p className="ml-5 mt-1 text-[11px] text-gray-500">
                When on, ECAPA voice embeddings are matched against enrolled SpeakerProfiles —
                each diarized cluster picks up a known person's name when confidence is high enough.
              </p>
            )}
          </div>

          {/* Summary template */}
          <div>
            <label className="mb-1 flex items-center gap-2 text-xs font-medium text-gray-300">
              <FileText className="h-3.5 w-3.5 text-amber-400" />
              Summary style
            </label>
            <div className="grid grid-cols-3 gap-2">
              {(['standard', 'executive', 'technical'] as const).map((tpl) => (
                <button
                  key={tpl}
                  type="button"
                  onClick={() => set('summary_template', tpl)}
                  className={`rounded border px-3 py-2 text-xs capitalize ${
                    summaryTpl === tpl
                      ? 'border-amber-500 bg-amber-500/10 text-amber-200'
                      : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                  }`}
                >
                  {tpl}
                </button>
              ))}
            </div>
          </div>

          {/* LLM model + thinking mode */}
          <div>
            <label className="mb-1 flex items-center gap-2 text-xs font-medium text-gray-300">
              <Brain className="h-3.5 w-3.5 text-pink-400" />
              Summarization model
            </label>
            <select
              value={value.llm_model ?? ''}
              onChange={(e) => set('llm_model', e.target.value || null)}
              className="w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white focus:border-pink-500 focus:outline-none"
            >
              <option value="">
                {defaultLlmLabel
                  ? `Use backend default (${defaultLlmLabel})`
                  : 'Use backend default'}
              </option>
              {(llmModels.length ? llmModels : FALLBACK_LLM_MODELS.filter((m) => m.id)).map((m) => {
                const route = 'route' in m ? (m as AvailableLlmModel).route : 'litellm';
                const isDefault = 'default' in m && (m as AvailableLlmModel).default;
                return (
                  <option key={m.id} value={m.id}>
                    {m.label}
                    {isDefault ? ' (current default)' : ''}
                    {route === 'direct' ? ' [direct]' : ''}
                  </option>
                );
              })}
            </select>
            {llmModels.length === 0 && (
              <p className="mt-1 text-[11px] text-gray-500">
                Model list unavailable — backend default will be used.
              </p>
            )}
            <label className="mt-2 flex items-center gap-2 text-xs text-gray-300">
              <input
                type="checkbox"
                checked={value.llm_thinking === true}
                onChange={(e) => set('llm_thinking', e.target.checked ? true : null)}
                className="h-3.5 w-3.5 rounded border-gray-600 bg-gray-800 text-pink-500"
              />
              <span>
                Enable model thinking traces (Qwen 3.5+ / Gemma 4)
                <span className="ml-1 text-[11px] text-gray-500">
                  (default off — thinking traces bloat output)
                </span>
              </span>
            </label>
          </div>
        </div>
      )}
    </div>
  );
};

/** Strip null / undefined values before sending to the backend. */
export function serializeTranscriptionOptions(
  opts: TranscriptionOptions,
): Record<string, any> | undefined {
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(opts)) {
    if (v !== null && v !== undefined) out[k] = v;
  }
  return Object.keys(out).length ? out : undefined;
}

export default TranscriptionOptionsPanel;
