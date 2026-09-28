import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { config } from '../config';
import { useAuth } from '../contexts/AuthContext';
import { useOrg } from '../contexts/OrgContext';
import { useUploads } from '../contexts/UploadsContext';
import { HeroActions } from '../components/dashboard/HeroActions';
import { StatsRow } from '../components/dashboard/StatsRow';
import { RecentMeetings } from '../components/dashboard/RecentMeetings';
import { RecentActionItems } from '../components/dashboard/RecentActionItems';
import { AskBar } from '../components/dashboard/AskBar';
import { PipelineFooter } from '../components/dashboard/PipelineFooter';
import OnboardingChecklist from '../components/OnboardingChecklist';
import { cacheKey, useHydratedState } from '../utils/cachedState';

export interface DashboardSession {
  id: string;
  name: string;
  title?: string;
  description?: string;
  created_at: string;
  duration?: number;
  status: string;
  participants?: Array<{ id: string; name: string; email?: string | null; role?: string | null }>;
  tags?: string[];
  summary_preview?: string | null;
  speaker_count?: number;
  project_app?: string | null;
  project_slug?: string | null;
  final_summary?: any;
  ai_insights?: any;
  summary?: any;
}

const SESSION_FETCH_LIMIT = 20;
const SESSION_DETAIL_LIMIT = 15;

export default function Dashboard() {
  const { user } = useAuth();
  const { activeOrganization } = useOrg();
  const { startUploads } = useUploads();
  // Hydrate from last-known-good sessions for this org so the dashboard
  // paints with real numbers immediately on refresh, even before the
  // network call comes back.
  const [sessions, setSessions, hydratedFromCache] = useHydratedState<DashboardSession[]>(
    cacheKey('dashboardSessions', activeOrganization?.slug),
    [],
  );
  // `loading` is only `true` until the first fetch resolves AND there's
  // nothing cached. Subsequent (polling) fetches don't toggle it, so
  // the visible UI never flips back into a "loading" state.
  const [loading, setLoading] = useState(!hydratedFromCache);
  const [detailsLoading, setDetailsLoading] = useState(!hydratedFromCache);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const buildHeaders = useCallback((): Record<string, string> => {
    const headers: Record<string, string> = {};
    const token = localStorage.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
    return headers;
  }, [activeOrganization?.slug]);

  const enrichWithDetails = useCallback(
    async (list: DashboardSession[]) => {
      if (list.length === 0) {
        setDetailsLoading(false);
        return;
      }
      const slice = list.slice(0, SESSION_DETAIL_LIMIT);
      const headers = buildHeaders();
      const results = await Promise.allSettled(
        slice.map((session) =>
          fetch(`${config.apiBaseUrl}/api/simple/recording-sessions/${session.id}`, { headers })
            .then((res) => (res.ok ? res.json() : null))
            .catch(() => null),
        ),
      );
      const detailById = new Map<string, any>();
      results.forEach((result, idx) => {
        if (result.status === 'fulfilled' && result.value) {
          detailById.set(slice[idx].id, result.value);
        }
      });
      setSessions((prev) =>
        prev.map((session) => {
          const detail = detailById.get(session.id);
          if (!detail) return session;
          return {
            ...session,
            final_summary: detail.final_summary ?? session.final_summary,
            ai_insights: detail.ai_insights ?? session.ai_insights,
            summary: detail.summary ?? session.summary,
          };
        }),
      );
      setDetailsLoading(false);
    },
    [buildHeaders],
  );

  const fetchSessions = useCallback(async () => {
    if (!activeOrganization?.slug) return;
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions?limit=${SESSION_FETCH_LIMIT}`,
        { headers: buildHeaders() },
      );
      if (!res.ok) {
        setError(`Failed to load sessions (HTTP ${res.status})`);
        setLoading(false);
        setDetailsLoading(false);
        return;
      }
      const data = await res.json();
      // The list endpoint now returns {items, next_cursor, has_more} (pagination,
      // performance-1/2/4); older shape was a bare array. Read .items first so the
      // Dashboard doesn't silently show zero sessions.
      const list: DashboardSession[] = Array.isArray(data)
        ? data
        : (data?.items ?? []);
      setSessions(list);
      setError(null);
      setLoading(false);
      setDetailsLoading(true);
      void enrichWithDetails(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load sessions');
      setLoading(false);
      setDetailsLoading(false);
    }
  }, [activeOrganization?.slug, buildHeaders, enrichWithDetails]);

  useEffect(() => {
    // Don't reset loading=true on every effect re-run — that throws away
    // the cached data we just hydrated. Only the very first mount sees
    // loading=true (set in useState init), and even then only if there
    // was nothing in the cache.
    fetchSessions();
    const id = window.setInterval(fetchSessions, 30000);
    return () => window.clearInterval(id);
  }, [fetchSessions]);

  const greeting = useMemo(() => {
    const name = user?.full_name?.split(' ')[0] || user?.username || user?.email || 'there';
    return `Welcome back, ${name}`;
  }, [user]);

  const triggerUploadPicker = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const handleUploadPicked = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      event.target.value = '';
      if (files.length === 0) return;
      void startUploads(files, { action: 'transcribe' });
    },
    [startUploads],
  );

  return (
    <div className="flex min-h-full flex-col bg-gradient-to-b from-zinc-950 to-black">
      <div className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:px-8">
        <header className="mb-6">
          <h1 className="text-2xl font-semibold text-white sm:text-3xl">{greeting}</h1>
          <p className="mt-1 text-sm text-zinc-400">
            {activeOrganization
              ? <>Workspace <span className="text-zinc-200">{activeOrganization.name}</span></>
              : 'Loading workspace…'}
          </p>
        </header>

        <HeroActions onUploadClick={triggerUploadPicker} />
        <input
          ref={fileInputRef}
          type="file"
          accept="audio/*,video/*"
          multiple
          className="hidden"
          onChange={handleUploadPicked}
        />

        <div className="mt-6">
          <AskBar />
        </div>

        {/* First-run onboarding checklist (4 dismissible cards). Hidden once
            the user has any sessions AND has dismissed every card. See
            OnboardingChecklist for the localStorage-backed visibility rule. */}
        <OnboardingChecklist sessionCount={sessions.length} />

        <div className="mt-8">
          <StatsRow sessions={sessions} loading={loading} detailsLoading={detailsLoading} />
        </div>

        <div className="mt-8 grid gap-6 lg:grid-cols-2">
          <RecentMeetings sessions={sessions} loading={loading} error={error} />
          <RecentActionItems sessions={sessions} loading={loading} detailsLoading={detailsLoading} />
        </div>

        <div className="mt-8">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Explore your knowledge</h2>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <a href="#/lectures" className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-200 transition hover:border-fuchsia-500/50 hover:text-white">
              Lectures
            </a>
            <a href="#/decisions-risks" className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-200 transition hover:border-fuchsia-500/50 hover:text-white">
              Decisions &amp; Risks
            </a>
            <a href="#/moments" className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-200 transition hover:border-fuchsia-500/50 hover:text-white">
              Important Moments
            </a>
            <a href="#/sessions" className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-200 transition hover:border-fuchsia-500/50 hover:text-white">
              All Meetings &amp; Documents
            </a>
          </div>
        </div>
      </div>
      <PipelineFooter />
    </div>
  );
}
