import React, { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Activity, Mic, Upload } from 'lucide-react';
import { toast } from 'react-toastify';
import { useAlwaysOn } from '../../contexts/AlwaysOnContext';

interface HeroActionsProps {
  onUploadClick: () => void;
}

export const HeroActions: React.FC<HeroActionsProps> = ({ onUploadClick }) => {
  const navigate = useNavigate();
  const alwaysOn = useAlwaysOn();

  const startAlwaysOn = useCallback(async () => {
    navigate('/record');
    if (alwaysOn.state === 'idle') {
      try {
        await alwaysOn.start();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : 'Failed to start always-on');
      }
    }
  }, [alwaysOn, navigate]);

  const quickRecord = useCallback(() => {
    navigate('/record');
  }, [navigate]);

  const isAlwaysOnLive = alwaysOn.state === 'recording' || alwaysOn.state === 'paused';

  return (
    <div className="flex flex-wrap gap-3">
      <button
        type="button"
        onClick={startAlwaysOn}
        disabled={alwaysOn.state === 'starting' || alwaysOn.state === 'stopping'}
        className="group relative inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-red-600 to-red-500 px-5 py-3 text-sm font-semibold text-white shadow-lg shadow-red-600/20 transition hover:from-red-500 hover:to-red-400 disabled:cursor-not-allowed disabled:opacity-60 sm:flex-none"
      >
        <span
          className={`flex h-2.5 w-2.5 rounded-full bg-white/90 ${isAlwaysOnLive ? 'animate-pulse' : ''}`}
        />
        {isAlwaysOnLive ? 'Always-on running' : 'Start always-on'}
      </button>

      <button
        type="button"
        onClick={quickRecord}
        className="inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-xl border border-zinc-700 bg-zinc-900 px-5 py-3 text-sm font-semibold text-zinc-100 transition hover:border-zinc-600 hover:bg-zinc-800 sm:flex-none"
      >
        <Mic className="h-4 w-4" />
        Quick record
      </button>

      <button
        type="button"
        onClick={onUploadClick}
        className="inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-xl border border-zinc-700 bg-zinc-900 px-5 py-3 text-sm font-semibold text-zinc-100 transition hover:border-zinc-600 hover:bg-zinc-800 sm:flex-none"
      >
        <Upload className="h-4 w-4" />
        Upload audio
      </button>

      {alwaysOn.statusText && alwaysOn.state !== 'idle' && (
        <div className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs text-zinc-300">
          <Activity className="h-3.5 w-3.5 text-emerald-300" />
          {alwaysOn.statusText}
        </div>
      )}
    </div>
  );
};
