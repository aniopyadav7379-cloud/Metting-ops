import { useEffect, useState, useCallback, useRef } from 'react';
import { showToast } from './Toast';
import { RotateCw, XCircle, ExternalLink } from 'lucide-react';
import type { JobStatusResponse, JobStatusFile } from '../pages/Import';

interface Props {
  jobId: string;
  jobStatus: JobStatusResponse | null;
  onStatusUpdate: (status: JobStatusResponse) => void;
  onComplete: () => void;
  onCancel: () => void;
  onRetryFailed: (fileId: string) => void;
}

const TERMINAL_STATES = new Set(['complete', 'cancelled', 'failed']);

function statusColor(status: string): string {
  switch (status) {
    case 'complete': return 'text-emerald-400';
    case 'failed': return 'text-red-400';
    case 'skipped': return 'text-amber-400';
    case 'processing':
    case 'uploading': return 'text-blue-400';
    default: return 'text-zinc-500';
  }
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'complete': return 'bg-emerald-500/10 text-emerald-400 border-emerald-800/30';
    case 'failed': return 'bg-red-500/10 text-red-400 border-red-800/30';
    case 'skipped': return 'bg-amber-500/10 text-amber-400 border-amber-800/30';
    case 'processing': return 'bg-blue-500/10 text-blue-400 border-blue-800/30';
    case 'uploading': return 'bg-zinc-500/10 text-zinc-400 border-zinc-800/30';
    default: return 'bg-zinc-500/10 text-zinc-500 border-zinc-800/30';
  }
}

export default function ImportProgressStage({
  jobId,
  jobStatus,
  onStatusUpdate,
  onComplete,
  onCancel,
  onRetryFailed,
}: Props) {
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState<Set<string>>(new Set());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const terminalRef = useRef(false);

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await fetch(`/api/import/jobs/${jobId}`, {
        credentials: 'include',
      });
      if (!resp.ok) throw new Error(`Status fetch failed: ${resp.status}`);
      const data = await resp.json();
      onStatusUpdate(data);
      if (TERMINAL_STATES.has(data.status) && !terminalRef.current) {
        terminalRef.current = true;
        if (intervalRef.current) clearInterval(intervalRef.current);
        onComplete();
      }
      return data;
    } catch {
      return null;
    }
  }, [jobId, onStatusUpdate, onComplete]);

  useEffect(() => {
    terminalRef.current = false;
    fetchStatus();
    intervalRef.current = setInterval(fetchStatus, 5000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchStatus]);

  const handleCancel = useCallback(async () => {
    setCancelling(true);
    try {
      const resp = await fetch(`/api/import/jobs/${jobId}/cancel`, {
        method: 'POST',
        credentials: 'include',
      });
      if (!resp.ok) throw new Error(`Cancel failed: ${resp.status}`);
      const data = await resp.json();
      onStatusUpdate(data);
    } catch (e) {
      showToast.error(e instanceof Error ? e.message : 'Cancel failed');
    } finally {
      setCancelling(false);
    }
  }, [jobId, onStatusUpdate]);

  const handleRetryFile = useCallback(
    async (fileId: string) => {
      setRetrying((prev) => new Set(prev).add(fileId));
      try {
        const resp = await fetch(
          `/api/import/jobs/${jobId}/files/${fileId}/retry`,
          { method: 'POST', credentials: 'include' },
        );
        if (!resp.ok) throw new Error(`Retry failed: ${resp.status}`);
        const data = await resp.json();
        onStatusUpdate(data);
        showToast.success('File re-queued for processing');
      } catch (e) {
        showToast.error(e instanceof Error ? e.message : 'Retry failed');
      } finally {
        setRetrying((prev) => {
          const next = new Set(prev);
          next.delete(fileId);
          return next;
        });
      }
    },
    [jobId, onStatusUpdate],
  );

  const progress = jobStatus
    ? {
        done: jobStatus.succeeded + jobStatus.failed + jobStatus.skipped,
        total: jobStatus.total_files || 1,
        pct:
          jobStatus.total_files > 0
            ? Math.round(
                ((jobStatus.succeeded + jobStatus.failed + jobStatus.skipped) /
                  jobStatus.total_files) *
                  100,
              )
            : 0,
      }
    : { done: 0, total: 1, pct: 0 };

  return (
    <div className="space-y-4">
      {/* Cancel button */}
      {jobStatus && !TERMINAL_STATES.has(jobStatus.status) && (
        <div className="flex justify-end">
          <button
            onClick={handleCancel}
            disabled={cancelling}
            className="flex items-center gap-1.5 rounded-lg border border-red-800 px-3 py-2 text-sm text-red-400 hover:bg-red-950/30 disabled:opacity-50"
          >
            <XCircle className="h-4 w-4" />
            {cancelling ? 'Cancelling...' : 'Cancel job'}
          </button>
        </div>
      )}

      {/* Progress bar */}
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span className="text-zinc-300">
            Status:{' '}
            <span className={statusColor(jobStatus?.status ?? 'queued')}>
              {jobStatus?.status ?? 'queued'}
            </span>
          </span>
          <span className="text-zinc-400">
            {jobStatus?.succeeded ?? 0} done &middot; {jobStatus?.failed ?? 0} failed
            &middot; {jobStatus?.skipped ?? 0} skipped &middot;{' '}
            {jobStatus?.total_files ?? 0} total
          </span>
        </div>
        <div className="h-2.5 overflow-hidden rounded-full bg-zinc-800">
          <div
            className="h-full rounded-full bg-fuchsia-500 transition-all duration-500"
            style={{ width: `${progress.pct}%` }}
          />
        </div>
        <p className="mt-1 text-right text-xs text-zinc-500">{progress.pct}%</p>
      </div>

      {/* Per-file status list */}
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-zinc-800 bg-zinc-900/80 text-xs uppercase text-zinc-500">
            <tr>
              <th className="px-3 py-3">File</th>
              <th className="px-3 py-3">Parsed title</th>
              <th className="px-3 py-3">Status</th>
              <th className="px-3 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {(jobStatus?.files ?? []).map((f: JobStatusFile) => (
              <tr key={f.file_id} className="hover:bg-zinc-800/40">
                <td
                  className="max-w-56 truncate px-3 py-2.5 text-zinc-300"
                  title={f.original_filename}
                >
                  {f.original_filename}
                </td>
                <td className="px-3 py-2.5 text-zinc-400">
                  {f.parsed_title || '-'}
                </td>
                <td className="px-3 py-2.5">
                  <span
                    className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${statusBadgeClass(
                      f.status,
                    )}`}
                  >
                    {f.status}
                  </span>
                  {f.error_message && (
                    <p
                      className="mt-1 max-w-xs truncate text-xs text-red-400"
                      title={f.error_message}
                    >
                      {f.error_message}
                    </p>
                  )}
                </td>
                <td className="px-3 py-2.5">
                  <div className="flex items-center gap-2">
                    {f.session_id && (
                      <a
                        href={`#/sessions/${f.session_id}`}
                        className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
                      >
                        <ExternalLink className="h-3 w-3" />
                        session
                      </a>
                    )}
                    {f.status === 'failed' && (
                      <button
                        onClick={() => handleRetryFile(f.file_id)}
                        disabled={retrying.has(f.file_id)}
                        className="flex items-center gap-1 text-xs text-amber-400 hover:text-amber-300 disabled:opacity-40"
                      >
                        <RotateCw
                          className={`h-3 w-3 ${
                            retrying.has(f.file_id) ? 'animate-spin' : ''
                          }`}
                        />
                        retry
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
