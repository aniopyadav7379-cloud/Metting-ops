import { RotateCw, Plus, ExternalLink } from 'lucide-react';
import type { JobStatusResponse } from '../pages/Import';

interface Props {
  jobStatus: JobStatusResponse | null;
  onRetryAll: () => void;
  onNewImport: () => void;
}

export default function ImportCompletionStage({
  jobStatus,
  onRetryAll,
  onNewImport,
}: Props) {
  const s = jobStatus;
  const succeeded = s?.succeeded ?? 0;
  const failed = s?.failed ?? 0;
  const skipped = s?.skipped ?? 0;

  const hasFailed = failed > 0;

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-6">
        <h2 className="mb-4 text-base font-medium">Import complete</h2>

        <div className="grid grid-cols-3 gap-4">
          <div className="rounded-lg bg-emerald-500/10 p-4 text-center">
            <p className="text-2xl font-semibold text-emerald-400">{succeeded}</p>
            <p className="mt-1 text-xs text-zinc-400">succeeded</p>
          </div>
          <div className="rounded-lg bg-red-500/10 p-4 text-center">
            <p className="text-2xl font-semibold text-red-400">{failed}</p>
            <p className="mt-1 text-xs text-zinc-400">failed</p>
          </div>
          <div className="rounded-lg bg-amber-500/10 p-4 text-center">
            <p className="text-2xl font-semibold text-amber-400">{skipped}</p>
            <p className="mt-1 text-xs text-zinc-400">skipped (duplicates)</p>
          </div>
        </div>

        {hasFailed && (
          <div className="mt-4 rounded-lg border border-amber-800/40 bg-amber-950/20 p-3 text-sm text-amber-300">
            {failed} file{failed === 1 ? '' : 's'} failed. You can retry them or
            start a new import for the remaining files.
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {hasFailed && (
          <button
            onClick={onRetryAll}
            className="flex items-center gap-2 rounded-lg bg-amber-600 px-5 py-2 text-sm font-medium text-white hover:bg-amber-500"
          >
            <RotateCw className="h-4 w-4" />
            Retry failed ({failed})
          </button>
        )}
        {succeeded > 0 && (
          <a
            href={`#/sessions`}
            className="flex items-center gap-2 rounded-lg bg-blue-600 px-5 py-2 text-sm font-medium text-white hover:bg-blue-500"
          >
            <ExternalLink className="h-4 w-4" />
            Go to sessions
          </a>
        )}
        <button
          onClick={onNewImport}
          className="flex items-center gap-2 rounded-lg border border-zinc-700 px-5 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          <Plus className="h-4 w-4" />
          New import
        </button>
      </div>
    </div>
  );
}
