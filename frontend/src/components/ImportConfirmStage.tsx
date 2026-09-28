import { useState, useMemo, useCallback } from 'react';
import { showToast } from './Toast';
import type { StagedFile, JobStatusResponse } from '../pages/Import';

interface Props {
  staged: StagedFile[];
  jobId: string;
  onBack: () => void;
  onStarted: (status: JobStatusResponse) => void;
  onError: (err: string) => void;
  orgSlug: string | null;
}

function estimatedDurationBytes(bytes: number): { hours: number; minutes: number } {
  const minutes = bytes / (1024 * 1024);
  return { hours: Math.floor(minutes / 60), minutes: Math.round(minutes % 60) };
}

export default function ImportConfirmStage({
  staged, jobId, onBack, onStarted, onError, orgSlug,
}: Props) {
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });

  const selected = useMemo(() => staged.filter((f) => f.selected), [staged]);
  const totalBytes = selected.reduce((s, f) => s + f.file.size, 0);
  const est = estimatedDurationBytes(totalBytes);
  const estProcessingMin = Math.max(1, Math.round(est.minutes / 2));

  const startImport = useCallback(async () => {
    setUploading(true);
    setProgress({ done: 0, total: selected.length });
    try {
      for (let i = 0; i < selected.length; i++) {
        const sf = selected[i];
        const fd = new FormData();
        fd.append('audio', sf.file, sf.file.name);
        if (sf.overrides.title) fd.append('override_title', sf.overrides.title);
        if (sf.overrides.meetingDate) fd.append('override_meeting_date', sf.overrides.meetingDate);
        if (sf.overrides.meetingTime) fd.append('override_meeting_time', sf.overrides.meetingTime);

        const resp = await fetch(`/api/import/jobs/${jobId}/files`, {
          method: 'POST',
          body: fd,
          credentials: 'include',
        });
        if (!resp.ok) {
          const body = await resp.text();
          throw new Error(`Upload failed for ${sf.file.name}: ${resp.status} ${body}`);
        }
        setProgress({ done: i + 1, total: selected.length });
      }

      // Fetch the initial job status to kick off the progress stage
      const statusResp = await fetch(`/api/import/jobs/${jobId}`, {
        credentials: 'include',
      });
      if (!statusResp.ok) throw new Error(`Status fetch failed: ${statusResp.status}`);
      const status = await statusResp.json();
      onStarted(status);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      onError(msg);
      showToast.error(msg);
    } finally {
      setUploading(false);
    }
  }, [selected, jobId, onStarted, onError]);

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6">
        <h2 className="mb-4 text-base font-medium">Confirm import</h2>

        <div className="space-y-3 text-sm text-zinc-300">
          <p>
            About to import <strong>{selected.length}</strong> file
            {selected.length === 1 ? '' : 's'} into organization{' '}
            <code className="rounded bg-zinc-800 px-1.5 py-0.5 text-fuchsia-300">
              {orgSlug || 'current'}
            </code>
          </p>

          <div className="grid grid-cols-2 gap-4 rounded-lg bg-zinc-800/50 p-4">
            <div>
              <span className="text-xs text-zinc-500">Total audio</span>
              <p className="mt-0.5 text-sm">
                {est.hours > 0 ? `${est.hours}h ` : ''}{est.minutes}m
              </p>
            </div>
            <div>
              <span className="text-xs text-zinc-500">Expected processing</span>
              <p className="mt-0.5 text-sm">~{estProcessingMin} min</p>
            </div>
            <div>
              <span className="text-xs text-zinc-500">GPU host</span>
              <p className="mt-0.5 text-sm">midboy1 (Parakeet 1.1B)</p>
            </div>
            <div>
              <span className="text-xs text-zinc-500">Storage bucket</span>
              <p className="mt-0.5 text-sm">Garage meeting-ops-audio</p>
            </div>
          </div>

          {uploading && (
            <div className="mt-4">
              <div className="mb-1 flex justify-between text-xs text-zinc-400">
                <span>Uploading files...</span>
                <span>{progress.done}/{progress.total}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-zinc-800">
                <div
                  className="h-full rounded-full bg-fuchsia-500 transition-all duration-300"
                  style={{
                    width: `${(progress.done / progress.total) * 100}%`,
                  }}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center justify-between">
        <button
          onClick={onBack}
          disabled={uploading}
          className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
        >
          Back to preview
        </button>
        <button
          onClick={startImport}
          disabled={uploading}
          className="rounded-lg bg-fuchsia-600 px-6 py-2 text-sm font-medium text-white hover:bg-fuchsia-500 disabled:opacity-50"
        >
          {uploading
            ? `Uploading ${progress.done}/${progress.total}...`
            : `Start import (${selected.length})`}
        </button>
      </div>
    </div>
  );
}
