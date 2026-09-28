import { useEffect, useState } from 'react';
import { config } from '../../config';
import { useSettingsContext } from './SettingsContext';
import type { SectionProps } from './SettingsContext';

interface PipelineBlock {
  label?: string;
  model?: string;
  ready?: boolean;
}
interface PipelineInfo {
  llm?: PipelineBlock;
  stt?: PipelineBlock;
  diarization?: PipelineBlock;
}

export default function AISettings(_props: SectionProps) {
  const { settings, setSettings } = useSettingsContext();
  // Read-only view of what's actually running. The models are managed by the
  // deployment (env-driven LiteLLM provider) — they are NOT switchable from the
  // app, so we show the live engine status instead of a fake model picker.
  const [pipeline, setPipeline] = useState<PipelineInfo | null>(null);

  useEffect(() => {
    const token = localStorage.getItem('access_token');
    fetch(`${config.apiUrl}/api/system/pipeline`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => data && setPipeline(data))
      .catch(() => {});
  }, []);

  const EngineRow = ({
    label,
    value,
    ready,
  }: {
    label: string;
    value?: string;
    ready?: boolean;
  }) => (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-xs text-zinc-500">{label}</span>
      <span className="flex items-center gap-2 text-sm text-zinc-200">
        {typeof ready === 'boolean' && (
          <span
            title={ready ? 'Ready' : 'Not reachable'}
            className={`inline-block h-2 w-2 rounded-full ${ready ? 'bg-emerald-400' : 'bg-zinc-600'}`}
          />
        )}
        {value || '—'}
      </span>
    </div>
  );

  return (
    <div className="space-y-6">
      <div>
        <label className="mb-2 block text-sm font-medium text-zinc-300">
          AI Engine
        </label>
        <div className="divide-y divide-zinc-800 rounded-lg border border-zinc-700 bg-zinc-800/60 px-4 py-2">
          <EngineRow
            label="Summaries & chat"
            value={pipeline?.llm?.label || 'Qwen 3.6 35B-A3B-Vision'}
            ready={pipeline?.llm?.ready}
          />
          <EngineRow
            label="Transcription"
            value={pipeline?.stt?.label || 'Parakeet 1.1B'}
            ready={pipeline?.stt?.ready}
          />
          <EngineRow
            label="Diarization"
            value={pipeline?.diarization?.label || 'Pyannote 3.1'}
            ready={pipeline?.diarization?.ready}
          />
        </div>
        <p className="mt-1 text-xs text-zinc-500">
          Models run on Unicorn Commander’s local GPU stack and are managed by your
          deployment — there’s nothing to configure here.
        </p>
      </div>

      <div>
        <label className="mb-2 block text-sm font-medium text-zinc-300">
          Default Summary Format
        </label>
        <select
          value={settings.summaryFormat}
          onChange={(e) =>
            setSettings({ ...settings, summaryFormat: e.target.value })
          }
          className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-zinc-200"
        >
          <option value="executive">Executive Summary</option>
          <option value="minutes">Meeting Minutes</option>
          <option value="bullets">Bullet Points</option>
          <option value="detailed">Detailed Report</option>
          <option value="standup">Standup Format</option>
          <option value="interview">Interview Notes</option>
        </select>
      </div>

      <div className="space-y-3">
        <label className="flex items-center gap-3">
          <input
            type="checkbox"
            checked={settings.enableAI}
            onChange={(e) =>
              setSettings({ ...settings, enableAI: e.target.checked })
            }
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800"
          />
          <span className="text-sm text-zinc-300">Enable AI processing</span>
        </label>

        <label className="flex items-center gap-3">
          <input
            type="checkbox"
            checked={settings.autoGenerateActions}
            onChange={(e) =>
              setSettings({ ...settings, autoGenerateActions: e.target.checked })
            }
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800"
          />
          <span className="text-sm text-zinc-300">Auto-generate action items</span>
        </label>

        <label className="flex items-center gap-3">
          <input
            type="checkbox"
            checked={settings.speakerDiarization}
            onChange={(e) =>
              setSettings({ ...settings, speakerDiarization: e.target.checked })
            }
            className="h-4 w-4 rounded border-zinc-600 bg-zinc-800"
          />
          <span className="text-sm text-zinc-300">Speaker diarization</span>
        </label>

        {settings.speakerDiarization && (
          <div className="ml-7">
            <label className="mb-1 block text-xs text-zinc-400">Max Speakers</label>
            <input
              type="number"
              value={settings.maxSpeakers}
              onChange={(e) =>
                setSettings({ ...settings, maxSpeakers: parseInt(e.target.value) })
              }
              className="w-20 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-200"
              min="2"
              max="10"
            />
          </div>
        )}
      </div>
    </div>
  );
}
