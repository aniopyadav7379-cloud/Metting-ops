import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Send, RotateCcw, X, User, Loader2, CheckCircle2 } from 'lucide-react';
import { config } from '../../config';
import { useOrg } from '../../contexts/OrgContext';
import type { DashboardSession } from '../../pages/Dashboard';
import { extractActionItems, type NormalizedActionItem } from './actionItems';
import { formatLifecycleDate, formatLifecycleTimestamp } from '../../utils/lifecycleTimestamp';

interface RecentActionItemsProps {
  sessions: DashboardSession[];
  loading: boolean;
  detailsLoading: boolean;
  /** Dashboard defaults keep this compact; the dedicated workspace expands it. */
  limit?: number;
  status?: string;
  title?: string;
  description?: string;
  showViewAll?: boolean;
}

interface ActionItemRow {
  id: number;
  session_id: number;
  session_key: string;
  session_title: string | null;
  session_created_at: string | null;
  text: string;
  owner: string | null;
  due_date: string | null;
  status: string;
  project_ops_link_state: 'local_only' | 'proposed' | 'approved_linked' | 'rejected' | 'sync_failed';
  project_ops_task_url?: string | null;
  project_ops_task_status?: string | null;
  project_ops_last_synced_at?: string | null;
  project_ops_sync_error?: string | null;
  project_ops_retry_count?: number;
}

const DEFAULT_LIMIT = 10;

/**
 * Reads from the new /api/action-items endpoint (first-class table).
 *
 * Falls back to the legacy client-side parser when the API returns an
 * empty list while we still see action-item-bearing JSON on recent
 * sessions — that's the window between deploy and the backfill
 * migration running, OR for sessions whose summary predates the
 * persist_action_items wiring.
 *
 * Meeting-Ops does NOT own action-item completion — Project-Ops does. The row's
 * primary control therefore reflects its Project-Ops lifecycle state: unsent items
 * offer "Send to Project-Ops" (POST .../project-ops/requeue), failed ones offer a
 * retry, and once an item is linked its status is read-only here because Project-Ops
 * is the sole owner of it. "Dismiss" deletes an item that should not be tracked at
 * all, which is honest in a way that silently marking it "done" here was not.
 */
export const RecentActionItems: React.FC<RecentActionItemsProps> = ({
  sessions,
  loading,
  detailsLoading,
  limit = DEFAULT_LIMIT,
  status = 'todo',
  title = 'Recent action items',
  description = 'Send an item to Project-Ops to track it. Open the item to review it in the meeting.',
  showViewAll = true,
}) => {
  const { activeOrganization } = useOrg();
  const [rows, setRows] = useState<ActionItemRow[]>([]);
  const [fetchState, setFetchState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [updatingIds, setUpdatingIds] = useState<Set<number>>(new Set());

  const buildHeaders = useCallback((): Record<string, string> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = localStorage.getItem('access_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
    return headers;
  }, [activeOrganization?.slug]);

  const fetchRows = useCallback(async () => {
    if (!activeOrganization?.slug) return;
    setFetchState((prev) => (prev === 'ready' ? 'ready' : 'loading'));
    try {
      const query = new URLSearchParams({ limit: String(limit) });
      if (status) query.set('status', status);
      const res = await fetch(`${config.apiBaseUrl}/api/action-items?${query}`, { headers: buildHeaders() });
      if (!res.ok) {
        setFetchState('error');
        return;
      }
      const data = await res.json();
      setRows(Array.isArray(data) ? data : []);
      setFetchState('ready');
    } catch {
      setFetchState('error');
    }
  }, [activeOrganization?.slug, buildHeaders, limit, status]);

  useEffect(() => {
    void fetchRows();
    const id = window.setInterval(fetchRows, 30000);
    return () => window.clearInterval(id);
  }, [fetchRows]);

  // Legacy client-side fallback. Only consulted when the API returned no
  // rows but we DO see action items embedded in the session JSON columns —
  // that signals sessions that haven't been re-summarized since the new
  // table landed. Drop this once the backfill has reached every tenant.
  const fallbackItems = useMemo<NormalizedActionItem[]>(() => {
    if (fetchState !== 'ready' || rows.length > 0) return [];
    const out: NormalizedActionItem[] = [];
    for (const session of sessions) {
      const extracted = extractActionItems(session as any);
      out.push(...extracted);
    }
    out.sort((a, b) => {
      const ta = a.sessionCreatedAt ? new Date(a.sessionCreatedAt).getTime() : 0;
      const tb = b.sessionCreatedAt ? new Date(b.sessionCreatedAt).getTime() : 0;
      return tb - ta;
    });
    return out.slice(0, limit);
  }, [fetchState, limit, rows.length, sessions]);

  /** Hand one item to Project-Ops. The backend owns the lifecycle from here. */
  const sendToProjectOps = useCallback(
    async (id: number) => {
      setUpdatingIds((prev) => new Set(prev).add(id));
      try {
        const res = await fetch(
          `${config.apiBaseUrl}/api/action-items/${id}/project-ops/requeue`,
          { method: 'POST', headers: buildHeaders() },
        );
        if (res.ok) {
          const updated = (await res.json()) as ActionItemRow;
          setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...updated } : r)));
        } else {
          await fetchRows();
        }
      } catch {
        await fetchRows();
      } finally {
        setUpdatingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [buildHeaders, fetchRows],
  );

  /** Drop an item that should not be tracked anywhere. */
  const dismissItem = useCallback(
    async (id: number) => {
      setUpdatingIds((prev) => new Set(prev).add(id));
      setRows((prev) => prev.filter((r) => r.id !== id));
      try {
        const res = await fetch(`${config.apiBaseUrl}/api/action-items/${id}`, {
          method: 'DELETE',
          headers: buildHeaders(),
        });
        if (!res.ok) await fetchRows();
      } catch {
        await fetchRows();
      } finally {
        setUpdatingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [buildHeaders, fetchRows],
  );

  const apiLoading = fetchState === 'loading' || (fetchState === 'idle' && !!activeOrganization?.slug);
  const fallbackLoading = loading || detailsLoading;
  const showRows = rows.length > 0;
  const showFallback = !showRows && fallbackItems.length > 0;
  const isLoading = apiLoading && !showRows && !showFallback && fallbackLoading;
  const isEmpty = !isLoading && !showRows && !showFallback;

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/50">
      <header className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
            {title}
          </h2>
          <p className="mt-1 text-[11px] normal-case tracking-normal text-zinc-500">
            {description}
          </p>
        </div>
        {showViewAll && (
          <Link
            to="/action-items"
            className="text-xs font-medium text-fuchsia-300 transition hover:text-fuchsia-200"
          >
            View all
          </Link>
        )}
      </header>

      <div className="divide-y divide-zinc-800/80">
        {isLoading && (
          <div className="px-4 py-8 text-center text-sm text-zinc-500">Loading action items…</div>
        )}

        {isEmpty && (
          <div className="px-4 py-8 text-center text-sm text-zinc-500">
            No action items captured yet. They appear here once a meeting summary completes.
          </div>
        )}

        {showRows &&
          rows.map((item) => {
            const busy = updatingIds.has(item.id);
            const state = item.project_ops_link_state || 'local_only';
            const canSend = state === 'local_only' || state === 'sync_failed';
            const isLinked = state === 'approved_linked';
            const lastSynced = formatLifecycleTimestamp(item.project_ops_last_synced_at);
            return (
              <div key={item.id} className="px-4 py-3">
                <div className="flex items-start gap-3">
                  {/* Project-Ops owns the lifecycle. Unsent items can be handed over;
                      once linked, the status shown here is read-only. */}
                  {busy ? (
                    <span className="mt-0.5 flex-shrink-0 text-zinc-400">
                      <Loader2 className="h-4 w-4 animate-spin" />
                    </span>
                  ) : canSend ? (
                    <button
                      type="button"
                      onClick={() => sendToProjectOps(item.id)}
                      className="mt-0.5 flex-shrink-0 text-indigo-300 transition hover:text-indigo-200"
                      title={
                        state === 'sync_failed'
                          ? item.project_ops_sync_error
                            ? `Retry — last error: ${item.project_ops_sync_error}`
                            : 'Retry sending to Project-Ops'
                          : 'Send to Project-Ops'
                      }
                      aria-label={
                        state === 'sync_failed'
                          ? 'Retry sending this action item to Project-Ops'
                          : 'Send this action item to Project-Ops'
                      }
                    >
                      {state === 'sync_failed' ? (
                        <RotateCcw className="h-4 w-4" />
                      ) : (
                        <Send className="h-4 w-4" />
                      )}
                    </button>
                  ) : (
                    <span
                      className={`mt-0.5 flex-shrink-0 ${isLinked ? 'text-emerald-400' : 'text-zinc-500'}`}
                      title={
                        isLinked
                          ? 'Tracked in Project-Ops — status is owned there'
                          : 'Sent to Project-Ops, awaiting triage'
                      }
                      aria-label={isLinked ? 'Tracked in Project-Ops' : 'Awaiting Project-Ops triage'}
                    >
                      <CheckCircle2 className="h-4 w-4" />
                    </span>
                  )}
                  <div className="min-w-0 flex-1">
                    <Link
                      to={`/sessions/${item.session_key}?tab=action_items&actionItem=${item.id}`}
                      className="text-sm text-zinc-100 transition hover:text-fuchsia-200 hover:underline"
                      title="Open this action item in its meeting"
                    >
                      {item.text}
                    </Link>
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
                      {item.owner && (
                        <span className="inline-flex items-center gap-1">
                          <User className="h-3 w-3" />
                          {item.owner}
                        </span>
                      )}
                      {formatLifecycleDate(item.due_date) && (
                        <span className="text-zinc-400">
                          Due {formatLifecycleDate(item.due_date)}
                        </span>
                      )}
                      {item.session_title && (
                        <Link
                          to={`/sessions/${item.session_key}`}
                          className="inline-flex max-w-[18ch] items-center gap-1 truncate rounded-full border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-300 transition hover:border-fuchsia-500/40 hover:text-fuchsia-200"
                          title={item.session_title}
                        >
                          {item.session_title}
                        </Link>
                      )}
                      {item.project_ops_link_state &&
                        item.project_ops_link_state !== 'local_only' && (
                          <>
                            <span
                              className="rounded-full border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-300"
                              title={
                                item.project_ops_link_state === 'sync_failed'
                                  ? item.project_ops_sync_error || 'Project-Ops sync failed'
                                  : undefined
                              }
                            >
                              Project-Ops{' '}
                              {item.project_ops_link_state === 'approved_linked'
                                ? `linked${item.project_ops_task_status ? ` · ${item.project_ops_task_status}` : ''}`
                                : item.project_ops_link_state.replace('_', ' ')}
                            </span>
                            {item.project_ops_task_url && (
                              <a
                                href={item.project_ops_task_url}
                                target="_blank"
                                rel="noreferrer"
                                onClick={(event) => event.stopPropagation()}
                                className="font-medium text-indigo-300 transition hover:text-indigo-200 hover:underline"
                              >
                                Open in Project-Ops
                              </a>
                            )}
                            {lastSynced && (
                              <span title={`Last successful sync ${lastSynced}`}>
                                synced{item.project_ops_retry_count ? ` · ${item.project_ops_retry_count} retries` : ''}
                              </span>
                            )}
                          </>
                        )}
                    </div>
                  </div>
                    {!busy && (
                      <button
                        type="button"
                        onClick={() => dismissItem(item.id)}
                        className="mt-0.5 flex-shrink-0 text-zinc-600 transition hover:text-rose-300"
                        title="Dismiss — remove this action item entirely"
                        aria-label="Dismiss this action item"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    )}
                </div>
              </div>
            );
          })}

        {showFallback &&
          fallbackItems.map((item, idx) => (
            <div key={`fallback-${item.sessionId}-${idx}`} className="px-4 py-3 opacity-90">
              <div className="flex items-start gap-3">
                <Send className="mt-0.5 h-4 w-4 flex-shrink-0 text-zinc-600" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <Link
                    to={`/sessions/${item.sessionId}?tab=action_items`}
                    className="text-sm text-zinc-100 transition hover:text-fuchsia-200 hover:underline"
                    title="Open this action item in its meeting"
                  >
                    {item.text}
                  </Link>
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
                    {item.owner && (
                      <span className="inline-flex items-center gap-1">
                        <User className="h-3 w-3" />
                        {item.owner}
                      </span>
                    )}
                    {item.dueDate && <span className="text-zinc-400">Due {item.dueDate}</span>}
                    <Link
                      to={`/sessions/${item.sessionId}`}
                      className="inline-flex max-w-[18ch] items-center gap-1 truncate rounded-full border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-300 transition hover:border-fuchsia-500/40 hover:text-fuchsia-200"
                      title={item.sessionTitle}
                    >
                      {item.sessionTitle}
                    </Link>
                  </div>
                </div>
              </div>
            </div>
          ))}
      </div>
    </section>
  );
};
