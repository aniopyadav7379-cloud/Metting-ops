import React from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Briefcase, Clock, FileText, Users } from 'lucide-react';
import type { DashboardSession } from '../../pages/Dashboard';
import { TagChip } from '../TagChip';

interface RecentMeetingsProps {
  sessions: DashboardSession[];
  loading: boolean;
  error: string | null;
}

function formatDuration(seconds?: number): string {
  if (!seconds || seconds <= 0) return '—';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${Math.round(seconds)}s`;
}

function formatDate(value?: string): string {
  if (!value) return '';
  try {
    const d = new Date(value);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    const diffDays = Math.floor((now.getTime() - d.getTime()) / (24 * 60 * 60 * 1000));
    if (diffDays >= 0 && diffDays < 7) {
      return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch {
    return value;
  }
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1).trimEnd()}…`;
}

export const RecentMeetings: React.FC<RecentMeetingsProps> = ({ sessions, loading, error }) => {
  const visible = sessions.slice(0, 5);

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/50">
      <header className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
          Recent meetings
        </h2>
        <Link
          to="/sessions"
          className="text-xs font-medium text-fuchsia-300 transition hover:text-fuchsia-200"
        >
          View all
        </Link>
      </header>

      <div className="divide-y divide-zinc-800/80">
        {loading ? (
          <div className="px-4 py-8 text-center text-sm text-zinc-500">Loading meetings…</div>
        ) : error ? (
          <div className="px-4 py-8 text-center text-sm text-red-400">{error}</div>
        ) : visible.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-zinc-500">
            No meetings yet. Start a recording or upload audio to get going.
          </div>
        ) : (
          visible.map((session) => {
            const title = session.title || session.name || 'Untitled meeting';
            const snippet = session.summary_preview ? truncate(session.summary_preview, 100) : '';
            const speakerCount = session.speaker_count || 0;
            const tags = (session.tags || []).slice(0, 3);
            const hiddenTagCount = (session.tags?.length || 0) - tags.length;
            return (
              <Link
                key={session.id}
                to={`/sessions/${session.id}`}
                className="block px-4 py-3 transition hover:bg-zinc-800/40"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate text-sm font-semibold text-white" title={title}>
                        {title}
                      </h3>
                      {session.project_app && session.project_slug && (
                        <span
                          className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] ${
                            session.project_app === 'crisis-ops'
                              ? 'border-red-800/60 bg-red-900/20 text-red-300'
                              : 'border-purple-800/60 bg-purple-900/20 text-purple-300'
                          }`}
                          title={`Linked to ${session.project_app} / ${session.project_slug}`}
                        >
                          {session.project_app === 'crisis-ops' ? (
                            <AlertTriangle className="h-2.5 w-2.5" />
                          ) : (
                            <Briefcase className="h-2.5 w-2.5" />
                          )}
                          {session.project_slug}
                        </span>
                      )}
                    </div>
                    {snippet && (
                      <p className="mt-1 line-clamp-2 text-xs text-zinc-400">{snippet}</p>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
                      <span className="inline-flex items-center gap-1">
                        <Clock className="h-3 w-3" />
                        {formatDuration(session.duration)}
                      </span>
                      {speakerCount > 0 && (
                        <span className="inline-flex items-center gap-1">
                          <Users className="h-3 w-3" />
                          {speakerCount} speaker{speakerCount === 1 ? '' : 's'}
                        </span>
                      )}
                      <span className="inline-flex items-center gap-1">
                        <FileText className="h-3 w-3" />
                        {formatDate(session.created_at)}
                      </span>
                    </div>
                    {tags.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {tags.map((tag) => (
                          <TagChip key={tag} tag={tag} />
                        ))}
                        {hiddenTagCount > 0 && (
                          <span className="rounded-full border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">
                            +{hiddenTagCount}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </Link>
            );
          })
        )}
      </div>
    </section>
  );
};
