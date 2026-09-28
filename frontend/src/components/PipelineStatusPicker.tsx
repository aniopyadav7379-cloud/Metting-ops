import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronDown, Cpu, FileText, Mic, Save, Settings, Users, Zap } from 'lucide-react';
import { config } from '../config';
import { useOrg } from '../contexts/OrgContext';
import {
  getAvailableModels,
  getStoredModelId,
  getWebGPUStatus,
  refreshAvailableModels,
  setStoredModelId,
  type InBrowserModelOption,
} from '../services/inBrowserLLM';
import {
  getSTTCapability,
  getSTTMode,
  setSTTMode,
  type SttMode,
} from '../services/inBrowserSTTSettings';
import { getLocalOnly } from '../services/privacyMode';

/**
 * Per-pipeline-stage dropdown wired to the same provider sources as the
 * AI Providers / In-Browser AI settings panels. Selection is per-session
 * by default — it writes to the same localStorage the always-on engine
 * already reads at start(). A small "Save as default" button promotes
 * the choice to the org's AI Providers settings (PUT /api/providers/{kind}).
 *
 * Constraints honored here:
 *   - Privacy mode (`localOnly`): hides server options for STT + LLM.
 *   - WebGPU missing: hides browser options for STT + LLM.
 *   - The Recording row stays read-only — it's UI state, not a model.
 *   - Diarization "Off" is a per-session toggle persisted to
 *     localStorage (`meetingops.pipeline.diarization.off.v1`).
 *
 * The component reads `/api/system/pipeline` for the live system label
 * (which model the BACKEND is wired to right now) and surfaces a small
 * "custom for this session" indicator when the user's per-session
 * selection differs from that org default.
 */

const DIARIZATION_OFF_KEY = 'meetingops.pipeline.diarization.off.v1';
const DIARIZATION_USER_OVERRIDE_KEY = 'meetingops.pipeline.diarization.user-override.v1';

function getDiarizationOff(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(DIARIZATION_OFF_KEY) === '1';
  } catch {
    return false;
  }
}

function setDiarizationOff(off: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(DIARIZATION_OFF_KEY, off ? '1' : '0');
    window.localStorage.setItem(DIARIZATION_USER_OVERRIDE_KEY, '1');
  } catch {
    /* noop */
  }
}

function getDiarizationUserOverride(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(DIARIZATION_USER_OVERRIDE_KEY) === '1';
  } catch {
    return false;
  }
}

function clearDiarizationUserOverride(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(DIARIZATION_USER_OVERRIDE_KEY);
  } catch {
    /* noop */
  }
}

interface BackendLlmModel {
  id: string;
  label: string;
  route: 'direct' | 'litellm';
  ready: boolean;
  default: boolean;
}

interface PipelineSnapshot {
  stt: {
    engine: string;
    model: string;
    ready: boolean;
    label: string;
    gpu?: string | null;
  };
  diarization: {
    backend: string;
    ready: boolean;
    label: string;
    gpu?: string | null;
  };
  llm: {
    route: string;
    model: string;
    ready: boolean;
    label: string;
    thinking?: boolean;
  };
  available_llm_models?: BackendLlmModel[];
}

interface DropdownOption {
  value: string;
  label: string;
  sublabel?: string;
  tier: 'browser' | 'server';
}

interface RowProps {
  icon: React.ReactNode;
  title: string;
  options: DropdownOption[];
  value: string;
  onChange: (next: string) => void;
  /** Right-aligned helper text — usually the GPU or route. */
  sublabel?: string;
  /** When true, this row's selection differs from the org default. */
  customForSession?: boolean;
  /** When true, the dropdown is disabled (e.g. no options). */
  disabled?: boolean;
  /** Indicator dot color (green = ready, yellow = warming, red = down). */
  statusColor?: string;
}

function Row({
  icon,
  title,
  options,
  value,
  onChange,
  sublabel,
  customForSession,
  disabled,
  statusColor,
}: RowProps) {
  const safeValue = options.find((o) => o.value === value) ? value : options[0]?.value ?? '';
  return (
    <div className="p-3 bg-zinc-800/50 rounded-lg">
      <div className="flex items-center gap-2 mb-1.5">
        {icon}
        <span className="text-xs text-zinc-400">{title}</span>
        {statusColor && (
          <span
            className={`ml-auto inline-block h-1.5 w-1.5 rounded-full ${statusColor}`}
            aria-hidden="true"
          />
        )}
      </div>
      <div className="relative">
        <select
          value={safeValue}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled || options.length === 0}
          className="w-full appearance-none rounded-md border border-zinc-700 bg-zinc-900/80 px-2.5 py-1.5 pr-7 text-sm text-zinc-100 focus:border-purple-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
        >
          {options.length === 0 ? (
            <option value="">No options available</option>
          ) : (
            options.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
                {opt.sublabel ? ` -- ${opt.sublabel}` : ''}
              </option>
            ))
          )}
        </select>
        <ChevronDown
          className="pointer-events-none absolute right-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500"
          aria-hidden="true"
        />
      </div>
      <div className="mt-1 flex items-center justify-between text-[11px]">
        <span className="text-zinc-500 truncate">{sublabel ?? ''}</span>
        {customForSession && (
          <span
            className="rounded-full border border-fuchsia-500/40 bg-fuchsia-500/10 px-1.5 py-0.5 text-[10px] font-medium text-fuchsia-200"
            title="This selection differs from the org default. Click Save as default below to promote it."
          >
            custom
          </span>
        )}
      </div>
    </div>
  );
}

interface PipelineStatusPickerProps {
  /** When true, show the recording status tile + dropdowns. */
  isRecording: boolean;
  recordingDurationLabel: string;
  /** Activated agent label rendered above the rows. */
  agentName?: string;
  agentDescription?: string;
  agentModelLabel?: string;
}

export default function PipelineStatusPicker({
  isRecording,
  recordingDurationLabel,
  agentName,
  agentDescription,
  agentModelLabel,
}: PipelineStatusPickerProps) {
  const { activeOrganization } = useOrg();
  const [pipeline, setPipeline] = useState<PipelineSnapshot | null>(null);
  const [browserLlmOptions, setBrowserLlmOptions] = useState<InBrowserModelOption[]>(
    () => getAvailableModels(),
  );

  // Per-session selections. Initial values come from localStorage (set
  // by the in-browser AI settings panel). On change we write back to
  // localStorage so the always-on engine picks them up at next start().
  const [llmModelId, setLlmModelId] = useState<string>(() => getStoredModelId());
  const [sttMode, setSttModeState] = useState<SttMode>(() => getSTTMode());
  const [diarizationOff, setDiarizationOffState] = useState<boolean>(() => getDiarizationOff());
  // Org-level default for diarization. "off" means new sessions skip
  // diarization unless the user explicitly overrides per-session.
  const [orgDiarizationDefault, setOrgDiarizationDefault] = useState<string>('local_diarization');

  // Capability flags -- privacy mode forces browser; WebGPU absence forces server.
  const [localOnly, setLocalOnlyState] = useState<boolean>(() => getLocalOnly());
  const sttCapable = useMemo(() => getSTTCapability().available, []);
  const webGpuStatus = useMemo(() => getWebGPUStatus(), []);
  const webGpuAvailable = !webGpuStatus.forcedServer;

  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  // Advanced panel collapsed by default — model/engine pickers are power-user controls.
  const [open, setOpen] = useState(false);

  // Read org-level diarization default. The "Save as default" button
  // writes here; the chunk-upload path also reads the same setting via
  // the backend `_org_diarization_off` helper. We mirror it into the
  // per-session localStorage flag so the dropdown shows the right initial
  // state, and so the chunk upload picks it up without an extra round-trip.
  useEffect(() => {
    let cancelled = false;
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
    (async () => {
      try {
        const res = await fetch(`${config.apiBaseUrl}/api/providers/diarization`, { headers });
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const provider = (data?.provider_name || 'local_diarization').toLowerCase();
        if (cancelled) return;
        setOrgDiarizationDefault(provider);
        // Only mirror into the per-session flag if the user hasn't
        // explicitly overridden. This way a user who picked "On" for
        // a single session doesn't have their override stomped when the
        // org default is "off".
        if (!getDiarizationUserOverride()) {
          const off = provider === 'off';
          if (typeof window !== 'undefined') {
            try {
              window.localStorage.setItem(DIARIZATION_OFF_KEY, off ? '1' : '0');
            } catch {
              /* noop */
            }
          }
          setDiarizationOffState(off);
        }
      } catch {
        /* leave defaults */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeOrganization?.slug]);

  // Refresh the live pipeline snapshot every 30s so STT/diarization/LLM
  // labels reflect what the BACKEND is wired to right now.
  useEffect(() => {
    let cancelled = false;
    const fetchPipeline = async () => {
      try {
        const res = await fetch(`${config.apiUrl}/api/system/pipeline`);
        if (!res.ok || cancelled) return;
        const data = (await res.json()) as PipelineSnapshot;
        if (!cancelled) setPipeline(data);
      } catch {
        /* leave stale */
      }
    };
    void fetchPipeline();
    const id = window.setInterval(fetchPipeline, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  // Pick up cross-tab changes from the in-browser AI settings panel.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'meetingops.inBrowserLLM.model.v1') setLlmModelId(getStoredModelId());
      if (e.key === 'meetingops.stt.mode.v1') setSttModeState(getSTTMode());
      if (e.key === 'meetingops.privacyMode.localOnly') setLocalOnlyState(getLocalOnly());
      if (e.key === DIARIZATION_OFF_KEY) setDiarizationOffState(getDiarizationOff());
    };
    const onPrivacyEvent = () => setLocalOnlyState(getLocalOnly());
    window.addEventListener('storage', onStorage);
    window.addEventListener('meetingops:privacy-mode', onPrivacyEvent as EventListener);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('meetingops:privacy-mode', onPrivacyEvent as EventListener);
    };
  }, []);

  // Web-llm catalog is fetched async; refresh once on mount.
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const next = await refreshAvailableModels();
        if (!cancelled) setBrowserLlmOptions(next);
      } catch {
        /* leave initial snapshot */
      }
    };
    void refresh();
    return () => {
      cancelled = true;
    };
  }, []);

  // --- Build dropdown options for each pipeline stage. --------------------
  const sttOptions = useMemo<DropdownOption[]>(() => {
    const out: DropdownOption[] = [];
    if (sttCapable) {
      out.push({
        value: 'browser-parakeet',
        label: 'Parakeet 0.6B INT8',
        sublabel: 'browser',
        tier: 'browser',
      });
    }
    if (!localOnly) {
      const serverLabel = pipeline?.stt?.label ?? 'NeMo Parakeet-TDT 1.1B';
      out.push({
        value: 'server-parakeet-1.1b',
        label: serverLabel,
        sublabel: 'server',
        tier: 'server',
      });
    }
    return out;
  }, [pipeline, localOnly, sttCapable]);

  const llmOptions = useMemo<DropdownOption[]>(() => {
    const out: DropdownOption[] = [];
    // Browser models first (snappier UX, default tier).
    if (webGpuAvailable) {
      for (const opt of browserLlmOptions) {
        if (opt.id === 'server') continue;
        out.push({
          value: opt.id,
          label: opt.label,
          sublabel: opt.runtime === 'web-llm' ? 'web-llm' : 'browser',
          tier: 'browser',
        });
      }
    }
    if (!localOnly) {
      // Server entry -- always include the active one even if available_llm_models
      // didn't come back (e.g. LiteLLM /models 401).
      out.push({
        value: 'server',
        label: pipeline?.llm?.label ?? 'Server LLM',
        sublabel: pipeline?.llm?.route ?? 'server',
        tier: 'server',
      });
      // Plus any additional models surfaced by /api/system/pipeline.
      const extras = pipeline?.available_llm_models ?? [];
      const seenIds = new Set(['server']);
      for (const m of extras) {
        if (!m?.id || seenIds.has(m.id)) continue;
        seenIds.add(m.id);
        // We surface backend-side alternates as `server:<id>`. The
        // AlwaysOnContext consumes `getStoredModelId()` and currently
        // dispatches 'server' to the server-side route; per-model
        // selection on the server side is a backend org-setting change
        // (Save as default), not a per-session in-browser flip.
        out.push({
          value: `server:${m.id}`,
          label: m.label,
          sublabel: m.route,
          tier: 'server',
        });
      }
    }
    return out;
  }, [pipeline, browserLlmOptions, localOnly, webGpuAvailable]);

  const diarizationOptions = useMemo<DropdownOption[]>(() => {
    const out: DropdownOption[] = [];
    if (!localOnly) {
      const label = pipeline?.diarization?.label ?? 'Pyannote 3.1 (multi-speaker)';
      out.push({
        value: 'server',
        label,
        sublabel: 'server',
        tier: 'server',
      });
    }
    out.push({
      value: 'off',
      label: 'Off (no speaker labels)',
      tier: 'browser',
    });
    return out;
  }, [pipeline, localOnly]);

  // --- Effective values respecting capability constraints ----------------
  const effectiveSttValue: string = useMemo(() => {
    if (localOnly && sttMode === 'server-parakeet-1.1b') return 'browser-parakeet';
    if (!sttCapable && sttMode === 'browser-parakeet') return 'server-parakeet-1.1b';
    return sttMode;
  }, [sttMode, localOnly, sttCapable]);

  const effectiveLlmValue: string = useMemo(() => {
    if (localOnly && llmModelId === 'server') return browserLlmOptions[0]?.id ?? 'server';
    if (!webGpuAvailable && llmModelId !== 'server' && !llmModelId.startsWith('server:'))
      return 'server';
    return llmModelId;
  }, [llmModelId, localOnly, webGpuAvailable, browserLlmOptions]);

  const effectiveDiarizationValue: string = diarizationOff ? 'off' : 'server';

  // --- Persistence handlers ---------------------------------------------
  const onStt = useCallback((next: string) => {
    if (next === 'browser-parakeet' || next === 'server-parakeet-1.1b') {
      setSTTMode(next as SttMode);
      setSttModeState(next as SttMode);
    }
  }, []);

  const onLlm = useCallback((next: string) => {
    // For backend "server:<model>" values we still persist 'server' as
    // the engine route -- the actual model picker is org-level and only
    // changes via "Save as default" which writes /api/providers/llm.
    if (next.startsWith('server:')) {
      setStoredModelId('server');
      setLlmModelId(next);
    } else {
      setStoredModelId(next);
      setLlmModelId(next);
    }
  }, []);

  const onDiarization = useCallback((next: string) => {
    const off = next === 'off';
    setDiarizationOff(off);
    setDiarizationOffState(off);
  }, []);

  // --- Save as default (org-level providers settings) -------------------
  const orgDefaultLlmId = useMemo(() => {
    const def = (pipeline?.available_llm_models ?? []).find((m) => m.default);
    return def?.id ?? pipeline?.llm?.model ?? null;
  }, [pipeline]);

  const isCustomLlm =
    !!orgDefaultLlmId &&
    llmModelId.startsWith('server:') &&
    llmModelId.slice('server:'.length) !== orgDefaultLlmId;
  // "Custom for this session" = current value differs from org default.
  const orgWantsOff = orgDiarizationDefault === 'off';
  const isCustomDiarization = diarizationOff !== orgWantsOff;

  const canSaveDefault = !!activeOrganization && (isCustomLlm || isCustomDiarization);

  const saveAsDefault = useCallback(async () => {
    if (!activeOrganization) return;
    setSaving(true);
    setSaveMessage(null);
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (activeOrganization.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
      if (isCustomLlm) {
        const model = llmModelId.startsWith('server:') ? llmModelId.slice('server:'.length) : null;
        if (model) {
          const res = await fetch(`${config.apiBaseUrl}/api/providers/llm`, {
            method: 'PUT',
            headers,
            body: JSON.stringify({
              provider_name: 'litellm',
              model_name: model,
              overrides: {},
            }),
          });
          if (!res.ok) throw new Error(`PUT /api/providers/llm -> ${res.status}`);
        }
      }
      if (isCustomDiarization) {
        const providerName = diarizationOff ? 'off' : 'local_diarization';
        const res = await fetch(`${config.apiBaseUrl}/api/providers/diarization`, {
          method: 'PUT',
          headers,
          body: JSON.stringify({
            provider_name: providerName,
            overrides: {},
          }),
        });
        if (!res.ok) throw new Error(`PUT /api/providers/diarization -> ${res.status}`);
        // The new org default now matches the user's session selection — clear
        // the override flag so the picker tracks the org default on next mount.
        setOrgDiarizationDefault(providerName);
        clearDiarizationUserOverride();
      }
      setSaveMessage('Saved as org default');
      // Refresh the pipeline so the "custom" indicator clears.
      const res = await fetch(`${config.apiUrl}/api/system/pipeline`);
      if (res.ok) {
        const data = (await res.json()) as PipelineSnapshot;
        setPipeline(data);
      }
    } catch (err: any) {
      setSaveMessage(err?.message || 'Failed to save default');
    } finally {
      setSaving(false);
      window.setTimeout(() => setSaveMessage(null), 4000);
    }
  }, [activeOrganization, isCustomLlm, isCustomDiarization, llmModelId, diarizationOff]);

  // --- Rendering --------------------------------------------------------
  return (
    <div className="rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-lg font-semibold text-white"
      >
        <Cpu className="w-5 h-5 text-purple-400" />
        Agent & Pipeline Status
        <span className="ml-auto text-xs font-normal text-zinc-400">{open ? 'Hide' : 'Advanced'}</span>
        <ChevronDown className={`h-4 w-4 text-zinc-400 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
      <div className="mt-4">

      {/* Active Agent identity (read-only for v1) */}
      <div className="mb-4 p-4 bg-purple-500/10 border border-purple-500/20 rounded-lg">
        <div className="flex items-center gap-3 mb-2">
          <Settings className="w-5 h-5 text-purple-400" />
          <div>
            <div className="text-sm font-medium text-white">{agentName ?? 'Meeting Assistant'}</div>
            <div className="text-xs text-purple-300">{agentModelLabel ?? pipeline?.llm?.label ?? 'AI Assistant'}</div>
          </div>
        </div>
        <div className="text-xs text-zinc-400">
          {agentDescription ?? 'Live + post-meeting summarization, action items, and Q&A.'}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {/* Recording -- local UI state only, NOT a dropdown */}
        <div className="p-3 bg-zinc-800/50 rounded-lg">
          <div className="flex items-center gap-2 mb-1">
            <Mic className={`w-4 h-4 ${isRecording ? 'text-red-400' : 'text-blue-400'}`} />
            <span className="text-xs text-zinc-400">Recording</span>
          </div>
          <div className={`text-sm font-medium ${isRecording ? 'text-red-300' : 'text-zinc-300'}`}>
            {isRecording ? 'Active' : 'Ready'}
          </div>
          <div className="text-xs text-zinc-500">{recordingDurationLabel}</div>
        </div>

        <Row
          icon={<FileText className="w-4 h-4 text-green-400" />}
          title="Transcription"
          options={sttOptions}
          value={effectiveSttValue}
          onChange={onStt}
          sublabel={
            effectiveSttValue === 'browser-parakeet'
              ? 'On-device (browser)'
              : pipeline?.stt?.gpu ?? pipeline?.stt?.engine
          }
          statusColor={pipeline?.stt?.ready ? 'bg-green-400' : 'bg-red-400'}
        />

        <Row
          icon={<Users className="w-4 h-4 text-emerald-400" />}
          title="Diarization"
          options={diarizationOptions}
          value={effectiveDiarizationValue}
          onChange={onDiarization}
          sublabel={diarizationOff ? 'disabled' : pipeline?.diarization?.gpu ?? pipeline?.diarization?.backend}
          customForSession={isCustomDiarization}
          statusColor={diarizationOff ? 'bg-zinc-500' : pipeline?.diarization?.ready ? 'bg-emerald-400' : 'bg-red-400'}
        />

        <Row
          icon={<Zap className="w-4 h-4 text-yellow-400" />}
          title="Summarizer"
          options={llmOptions}
          value={effectiveLlmValue}
          onChange={onLlm}
          sublabel={
            llmOptions.find((o) => o.value === effectiveLlmValue)?.tier === 'browser'
              ? 'On-device (browser)'
              : pipeline?.llm?.route
          }
          customForSession={isCustomLlm}
          statusColor={pipeline?.llm?.ready ? 'bg-yellow-400' : 'bg-red-400'}
        />
      </div>

      {/* Save as default -- only enabled when the user has picked a
          server-side LLM model that differs from the org default. The
          button is hidden when there's nothing meaningful to save (we
          don't want to write the same value back on every click). */}
      <div className="mt-4 flex items-center justify-between gap-2">
        <div className="text-[11px] text-zinc-500">
          {localOnly && 'Local-only mode -- server options hidden. '}
          {!webGpuAvailable && 'WebGPU unavailable -- browser options hidden. '}
          {!localOnly && webGpuAvailable && 'Selections apply to your next session.'}
        </div>
        {canSaveDefault && (
          <button
            type="button"
            onClick={saveAsDefault}
            disabled={saving}
            className="inline-flex items-center gap-1.5 rounded-md border border-fuchsia-500/40 bg-fuchsia-500/10 px-2.5 py-1 text-[11px] font-medium text-fuchsia-100 transition hover:bg-fuchsia-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Save className="h-3 w-3" />
            {saving ? 'Saving...' : 'Save as default'}
          </button>
        )}
      </div>
      {saveMessage && (
        <div className="mt-2 text-[11px] text-zinc-400">{saveMessage}</div>
      )}
      </div>
      )}
    </div>
  );
}
