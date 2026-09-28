import { useEffect, useState, useCallback } from 'react';
import { showToast } from '../components/Toast';
import {
  RotateCw, XCircle, PauseCircle, PlayCircle, ExternalLink,
  Search, AlertTriangle,
} from 'lucide-react';

interface AdminJobListItem {
  job_id: string;
  organization_id: number;
  user_id: number;
  user_email: string | null;
  org_name: string | null;
  status: string;
  total_files: number;
  succeeded: number;
  failed: number;
  skipped: number;
  started_at: string | null;
  finished_at: string | null;
  cancelled_at: string | null;
  created_at: string;
}

interface AdminJobListResponse {
  jobs: AdminJobListItem[];
  total: number;
}

interface AdminJobFile {
  file_id: string;
  original_filename: string;
  status: string;
  session_id: string | null;
  error_message: string | null;
  bytes_total: number | null;
  created_at: string | null;
}

interface AdminJobDetail {
  job_id: string;
  user_id: number;
  organization_id: number;
  status: string;
  total_files: number;
  succeeded: number;
  failed: number;
  skipped: number;
  started_at: string | null;
  finished_at: string | null;
  cancelled_at: string | null;
  created_at: string;
  files: AdminJobFile[];
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'complete': return 'bg-emerald-500/10 text-emerald-400 border-emerald-800/30';
    case 'failed': return 'bg-red-500/10 text-red-400 border-red-800/30';
    case 'cancelled': return 'bg-zinc-500/10 text-zinc-400 border-zinc-800/30';
    case 'paused': return 'bg-amber-500/10 text-amber-400 border-amber-800/30';
    case 'processing': return 'bg-blue-500/10 text-blue-400 border-blue-800/30';
    case 'queued': return 'bg-zinc-600/10 text-zinc-300 border-zinc-700/30';
    case 'pending': return 'bg-zinc-500/10 text-zinc-500 border-zinc-800/30';
    default: return 'bg-zinc-500/10 text-zinc-500 border-zinc-800/30';
  }
}

function statusColor(status: string): string {
  switch (status) {
    case 'complete': return 'text-emerald-400';
    case 'failed': return 'text-red-400';
    case 'cancelled': return 'text-zinc-400';
    case 'paused': return 'text-amber-400';
    case 'processing': return 'text-blue-400';
    case 'queued': return 'text-zinc-300';
    default: return 'text-zinc-500';
  }
}

const ALL_STATUSES = ['all', 'queued', 'processing', 'paused', 'complete', 'failed', 'cancelled'];

export default function AdminBulkImport() {
  const [jobs, setJobs] = useState<AdminJobListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('all');
  const [selectedJob, setSelectedJob] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<AdminJobDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const fetchJobs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (statusFilter !== 'all') params.set('status', statusFilter);
      params.set('limit', '100');
      const resp = await fetch(`/api/import/admin/jobs?${params}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: AdminJobListResponse = await resp.json();
      setJobs(data.jobs);
    } catch (err) {
      console.error('Failed to fetch admin jobs:', err);
      showToast.error('Failed to load bulk import jobs');
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  const fetchJobDetail = useCallback(async (jobId: string) => {
    setDetailLoading(true);
    try {
      const resp = await fetch(`/api/import/admin/jobs/${jobId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: AdminJobDetail = await resp.json();
      setJobDetail(data);
    } catch (err) {
      console.error('Failed to fetch job detail:', err);
      showToast.error('Failed to load job details');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  useEffect(() => {
    if (selectedJob) {
      fetchJobDetail(selectedJob);
    } else {
      setJobDetail(null);
    }
  }, [selectedJob, fetchJobDetail]);

  const handleAction = async (jobId: string, action: 'pause' | 'resume' | 'cancel') => {
    setActionLoading(`${action}-${jobId}`);
    try {
      const resp = await fetch(`/api/import/admin/jobs/${jobId}/${action}`, {
        method: 'POST',
      });
      if (!resp.ok) {
        const detail = await resp.text();
        throw new Error(detail || `HTTP ${resp.status}`);
      }
      showToast.success(`Job ${action}d successfully`);
      await fetchJobs();
      if (selectedJob === jobId) {
        await fetchJobDetail(jobId);
      }
    } catch (err) {
      showToast.error(`Failed to ${action} job: ${err}`);
    } finally {
      setActionLoading(null);
    }
  };

  const ActionButtons = ({ job }: { job: AdminJobListItem }) => {
    const isProcessing =
      actionLoading !== null &&
      actionLoading.startsWith(
        ['pause', 'resume', 'cancel'].find((a) =>
          actionLoading === `${a}-${job.job_id}`,
        ) || '',
      );

    return (
      <div className="flex gap-2">
        {job.status === 'processing' && (
          <button
            onClick={() => handleAction(job.job_id, 'pause')}
            disabled={actionLoading !== null}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-amber-500/10 text-amber-400 border border-amber-800/30 hover:bg-amber-500/20 disabled:opacity-50"
          >
            <PauseCircle className="w-3.5 h-3.5" />
            Pause
          </button>
        )}
        {job.status === 'paused' && (
          <button
            onClick={() => handleAction(job.job_id, 'resume')}
            disabled={actionLoading !== null}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-blue-500/10 text-blue-400 border border-blue-800/30 hover:bg-blue-500/20 disabled:opacity-50"
          >
            <PlayCircle className="w-3.5 h-3.5" />
            Resume
          </button>
        )}
        {(job.status === 'processing' || job.status === 'paused' || job.status === 'queued') && (
          <button
            onClick={() => handleAction(job.job_id, 'cancel')}
            disabled={actionLoading !== null}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-red-500/10 text-red-400 border border-red-800/30 hover:bg-red-500/20 disabled:opacity-50"
          >
            <XCircle className="w-3.5 h-3.5" />
            Cancel
          </button>
        )}
      </div>
    );
  };

  const FileStatusRow = ({ file }: { file: AdminJobFile }) => (
    <tr className="border-b border-zinc-800/50">
      <td className="py-2 px-3 text-sm text-zinc-300 truncate max-w-xs" title={file.original_filename}>
        {file.original_filename}
      </td>
      <td>
        <span className={`px-2 py-0.5 text-xs rounded border ${statusBadgeClass(file.status)}`}>
          {file.status}
        </span>
      </td>
      <td className="py-2 px-3 text-sm text-zinc-400">
        {file.session_id ? (
          <a
            href={`#/sessions/${file.session_id}`}
            className="text-blue-400 hover:text-blue-300 flex items-center gap-1"
          >
            <ExternalLink className="w-3 h-3" />
            View
          </a>
        ) : '-'}
      </td>
      <td className="py-2 px-3 text-sm text-red-400 max-w-xs truncate">
        {file.error_message || '-'}
      </td>
    </tr>
  );

  if (selectedJob && jobDetail) {
    return (
      <div className="p-6 max-w-6xl mx-auto">
        <button
          onClick={() => setSelectedJob(null)}
          className="mb-4 text-sm text-zinc-400 hover:text-white transition-colors"
        >
          &larr; Back to all jobs
        </button>

        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-4 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-white">Job {jobDetail.job_id.slice(0, 8)}...</h2>
            <span className={`px-3 py-1 text-sm rounded border ${statusBadgeClass(jobDetail.status)}`}>
              {jobDetail.status}
            </span>
          </div>
          <div className="grid grid-cols-4 gap-4 text-sm">
            <div>
              <div className="text-zinc-500">Total</div>
              <div className="text-white font-medium">{jobDetail.total_files}</div>
            </div>
            <div>
              <div className="text-zinc-500">Succeeded</div>
              <div className="text-emerald-400 font-medium">{jobDetail.succeeded}</div>
            </div>
            <div>
              <div className="text-zinc-500">Failed</div>
              <div className="text-red-400 font-medium">{jobDetail.failed}</div>
            </div>
            <div>
              <div className="text-zinc-500">Skipped</div>
              <div className="text-amber-400 font-medium">{jobDetail.skipped}</div>
            </div>
          </div>
          <div className="flex gap-3 mt-4">
            {jobDetail.status === 'processing' && (
              <button
                onClick={() => handleAction(jobDetail.job_id, 'pause')}
                disabled={actionLoading !== null}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded bg-amber-500/10 text-amber-400 border border-amber-800/30 hover:bg-amber-500/20 disabled:opacity-50"
              >
                <PauseCircle className="w-4 h-4" />
                Pause
              </button>
            )}
            {jobDetail.status === 'paused' && (
              <button
                onClick={() => handleAction(jobDetail.job_id, 'resume')}
                disabled={actionLoading !== null}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded bg-blue-500/10 text-blue-400 border border-blue-800/30 hover:bg-blue-500/20 disabled:opacity-50"
              >
                <PlayCircle className="w-4 h-4" />
                Resume
              </button>
            )}
            {(jobDetail.status === 'processing' || jobDetail.status === 'paused' || jobDetail.status === 'queued') && (
              <button
                onClick={() => handleAction(jobDetail.job_id, 'cancel')}
                disabled={actionLoading !== null}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded bg-red-500/10 text-red-400 border border-red-800/30 hover:bg-red-500/20 disabled:opacity-50"
              >
                <XCircle className="w-4 h-4" />
                Cancel
              </button>
            )}
          </div>
        </div>

        {detailLoading ? (
          <div className="flex items-center gap-2 text-zinc-400">
            <RotateCw className="w-4 h-4 animate-spin" />
            Loading files...
          </div>
        ) : (
          <div className="bg-zinc-900/30 border border-zinc-800 rounded-lg overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-zinc-800 text-left">
                  <th className="py-2 px-3 text-xs uppercase tracking-wider text-zinc-500 font-medium">Filename</th>
                  <th className="py-2 px-3 text-xs uppercase tracking-wider text-zinc-500 font-medium">Status</th>
                  <th className="py-2 px-3 text-xs uppercase tracking-wider text-zinc-500 font-medium">Session</th>
                  <th className="py-2 px-3 text-xs uppercase tracking-wider text-zinc-500 font-medium">Error</th>
                </tr>
              </thead>
              <tbody>
                {jobDetail.files.map((f) => (
                  <FileStatusRow key={f.file_id} file={f} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Bulk Import Jobs</h1>
          <p className="text-sm text-zinc-500 mt-1">Admin view: all jobs across all organizations</p>
        </div>
        <button
          onClick={fetchJobs}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-zinc-800 text-zinc-300 rounded-lg hover:bg-zinc-700 transition-colors disabled:opacity-50"
        >
          <RotateCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <div className="mb-4 flex gap-2">
        {ALL_STATUSES.map((s) => (
          <button
            key={s}
            onClick={() => setStatusFilter(s)}
            className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
              statusFilter === s
                ? 'bg-zinc-700 text-white border-zinc-600'
                : 'bg-zinc-900/50 text-zinc-400 border-zinc-800 hover:bg-zinc-800'
            }`}
          >
            {s.charAt(0).toUpperCase() + s.slice(1)}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-zinc-400 py-8">
          <RotateCw className="w-4 h-4 animate-spin" />
          Loading jobs...
        </div>
      ) : jobs.length === 0 ? (
        <div className="text-center py-12 text-zinc-500">
          <AlertTriangle className="w-8 h-8 mx-auto mb-2 opacity-50" />
          No bulk import jobs found.
        </div>
      ) : (
        <div className="bg-zinc-900/30 border border-zinc-800 rounded-lg overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-800 text-left">
                <th className="py-3 px-4 text-xs uppercase tracking-wider text-zinc-500 font-medium">Org</th>
                <th className="py-3 px-4 text-xs uppercase tracking-wider text-zinc-500 font-medium">User</th>
                <th className="py-3 px-4 text-xs uppercase tracking-wider text-zinc-500 font-medium">Status</th>
                <th className="py-3 px-4 text-xs uppercase tracking-wider text-zinc-500 font-medium">Progress</th>
                <th className="py-3 px-4 text-xs uppercase tracking-wider text-zinc-500 font-medium">Started</th>
                <th className="py-3 px-4 text-xs uppercase tracking-wider text-zinc-500 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr
                  key={job.job_id}
                  className="border-b border-zinc-800/50 hover:bg-zinc-800/20 cursor-pointer"
                  onClick={() => setSelectedJob(job.job_id)}
                >
                  <td className="py-3 px-4 text-sm text-zinc-300">{job.org_name || `Org #${job.organization_id}`}</td>
                  <td className="py-3 px-4 text-sm text-zinc-400">{job.user_email || `User #${job.user_id}`}</td>
                  <td className="py-3 px-4">
                    <span className={`px-2 py-0.5 text-xs rounded border ${statusBadgeClass(job.status)}`}>
                      {job.status}
                    </span>
                  </td>
                  <td className="py-3 px-4 text-sm text-zinc-300">
                    {job.succeeded}/{job.total_files}
                    {job.failed > 0 && <span className="text-red-400 ml-1">({job.failed} failed)</span>}
                    {job.skipped > 0 && <span className="text-amber-400 ml-1">({job.skipped} skipped)</span>}
                  </td>
                  <td className="py-3 px-4 text-sm text-zinc-400">
                    {job.started_at ? new Date(job.started_at).toLocaleString() : '-'}
                  </td>
                  <td className="py-3 px-4" onClick={(e) => e.stopPropagation()}>
                    <ActionButtons job={job} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
