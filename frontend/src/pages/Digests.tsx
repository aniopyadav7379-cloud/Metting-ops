import React, { useState, useEffect } from 'react';
import { Calendar, RefreshCw, FileText, Clock } from 'lucide-react';
import { useOrg } from '../contexts/OrgContext';
import { config } from '../config';
import { pollJob, JobPollError } from '../services/jobPoller';

interface DigestData {
  period: string;
  date: string;
  content: string;
  meeting_count: number;
  cached: boolean;
  project_id?: string | null;
}

const PERIODS = [
  { value: 'day', label: 'Today' },
  { value: 'week', label: 'This Week' },
  { value: 'month', label: 'This Month' },
  { value: 'quarter', label: 'This Quarter' },
  { value: 'year', label: 'This Year' },
];

export default function DigestsPage() {
  const { activeOrganization } = useOrg();
  const [period, setPeriod] = useState('month');
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10));
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [progressMsg, setProgressMsg] = useState('');

  const getHeaders = () => {
    const headers: Record<string, string> = {};
    if (activeOrganization?.slug) {
      headers['X-MeetingOps-Org'] = activeOrganization.slug;
    }
    return headers;
  };

  const fetchDigest = async (force = false) => {
    setLoading(true);
    setError('');
    setProgressMsg('');
    try {
      const params = new URLSearchParams({ period, date });
      if (force) params.set('force', 'true');
      const res = await fetch(`${config.apiBaseUrl}/api/digests?${params}`, {
        headers: getHeaders(),
      });
      if (res.status === 202) {
        // v3.18.3: digest generation is queued — poll for the result.
        const payload = await res.json();
        setProgressMsg('Generating digest...');
        try {
          const result = await pollJob<DigestData>(payload.job_id, {
            headers: getHeaders(),
            onProgress: (s) => {
              if (s.status === 'running') setProgressMsg('Summarizing meetings...');
              else if (s.status === 'pending') setProgressMsg('Waiting in queue...');
            },
          });
          setDigest(result);
        } catch (err) {
          if (err instanceof JobPollError) {
            setError(err.originalError || 'Digest generation failed.');
          } else {
            setError('Digest generation failed.');
          }
        }
      } else if (res.ok) {
        setDigest(await res.json());
      } else {
        setError('Failed to generate digest.');
      }
    } catch {
      setError('Network error.');
    }
    setLoading(false);
    setProgressMsg('');
  };

  useEffect(() => {
    setDate(new Date().toISOString().slice(0, 10));
  }, [period]);

  return (
    <div className="h-full flex flex-col bg-zinc-950 p-4">
      <div className="flex items-center gap-3 mb-4">
        <FileText className="w-5 h-5 text-fuchsia-400" />
        <h1 className="text-lg font-semibold text-zinc-100">Meeting Digests</h1>
      </div>

      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <select
          value={period}
          onChange={e => setPeriod(e.target.value)}
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none"
        >
          {PERIODS.map(p => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>
        <input
          type="date"
          value={date}
          onChange={e => setDate(e.target.value)}
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none"
        />
        <button
          onClick={() => fetchDigest(false)}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 disabled:opacity-40 text-white text-sm font-medium transition-colors"
        >
          <Calendar className="w-4 h-4" />
          Generate
        </button>
        <button
          onClick={() => fetchDigest(true)}
          disabled={loading}
          title="Force regeneration"
          className="p-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-zinc-400 py-8">
          <RefreshCw className="w-4 h-4 animate-spin" />
          <span>{progressMsg || 'Generating digest...'}</span>
        </div>
      )}

      {error && (
        <div className="bg-red-600/20 border border-red-500/30 rounded-lg px-4 py-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      {digest && !loading && (
        <div className="flex-1 overflow-y-auto">
          <div className="flex items-center gap-3 mb-3">
            <span className="text-xs text-zinc-500 uppercase tracking-wider px-2 py-0.5 bg-zinc-800 rounded">
              {period}
            </span>
            <span className="text-xs text-zinc-500">{date}</span>
            {digest.meeting_count > 0 && (
              <span className="text-xs text-zinc-500">
                {digest.meeting_count} meeting{digest.meeting_count !== 1 ? 's' : ''}
              </span>
            )}
          </div>
          <div className="rounded-lg bg-zinc-900 border border-zinc-800 p-4">
            <p className="text-sm text-zinc-200 whitespace-pre-wrap leading-relaxed">
              {digest.content}
            </p>
          </div>
        </div>
      )}

      {!digest && !loading && !error && (
        <div className="flex-1 flex flex-col items-center justify-center text-center py-16 px-6 border border-dashed border-zinc-800 rounded-lg">
          <FileText className="w-10 h-10 text-zinc-600 mb-3" />
          <p className="text-sm text-zinc-300 font-medium">No digest yet</p>
          <p className="text-xs text-zinc-500 mt-1 max-w-sm">
            Pick a period above and click <span className="text-zinc-300">Generate</span> to build a
            summary of your meetings for that range.
          </p>
        </div>
      )}
    </div>
  );
}
