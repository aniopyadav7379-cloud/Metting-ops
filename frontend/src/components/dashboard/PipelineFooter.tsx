import React, { useEffect, useState } from 'react';
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { config } from '../../config';

interface PipelinePart {
  label: string;
  model?: string;
  ready: boolean;
}

interface PipelineState {
  stt: PipelinePart;
  diarization: PipelinePart;
  llm: PipelinePart;
}

function ChipIcon({ ready }: { ready: boolean }) {
  return ready ? (
    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-300" />
  ) : (
    <XCircle className="h-3.5 w-3.5 text-red-300" />
  );
}

export const PipelineFooter: React.FC = () => {
  const [state, setState] = useState<PipelineState | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const headers: Record<string, string> = {};
        const token = localStorage.getItem('access_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const res = await fetch(`${config.apiBaseUrl}/api/system/pipeline`, { headers });
        if (!res.ok) {
          if (!cancelled) setLoading(false);
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        setState({
          stt: {
            label: 'STT',
            model: data.stt?.label || data.stt?.model || 'unknown',
            ready: Boolean(data.stt?.ready),
          },
          diarization: {
            label: 'Diarization',
            model: data.diarization?.label || data.diarization?.backend || 'unknown',
            ready: Boolean(data.diarization?.ready),
          },
          llm: {
            label: 'LLM',
            model: data.llm?.label || data.llm?.model || 'unknown',
            ready: Boolean(data.llm?.ready),
          },
        });
        setLoading(false);
      } catch {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const id = window.setInterval(load, 60000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return (
    <footer className="mt-8 border-t border-zinc-800 bg-zinc-950/80 backdrop-blur">
      <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center gap-3 px-4 py-3 text-xs text-zinc-400 sm:px-6 lg:px-8">
        <span className="font-medium uppercase tracking-wide text-zinc-500">Pipeline</span>
        {loading ? (
          <span className="inline-flex items-center gap-1.5 text-zinc-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            checking
          </span>
        ) : state ? (
          <>
            <span className="inline-flex items-center gap-1.5">
              <ChipIcon ready={state.stt.ready} />
              <span className="text-zinc-300">STT</span>
              <span className="text-zinc-500">·</span>
              <span className="truncate text-zinc-400">{state.stt.model}</span>
            </span>
            <span className="text-zinc-700">|</span>
            <span className="inline-flex items-center gap-1.5">
              <ChipIcon ready={state.diarization.ready} />
              <span className="text-zinc-300">Diarization</span>
              <span className="text-zinc-500">·</span>
              <span className="truncate text-zinc-400">{state.diarization.model}</span>
            </span>
            <span className="text-zinc-700">|</span>
            <span className="inline-flex items-center gap-1.5">
              <ChipIcon ready={state.llm.ready} />
              <span className="text-zinc-300">LLM</span>
              <span className="text-zinc-500">·</span>
              <span className="truncate text-zinc-400">{state.llm.model}</span>
            </span>
          </>
        ) : (
          <span className="text-zinc-500">unavailable</span>
        )}
      </div>
    </footer>
  );
};
