import { useCallback, useEffect, useRef, useState } from 'react';

export type FederationSummaryApprovalStatus =
  | 'approved'
  | 'unapproved'
  | 'stale'
  | 'unavailable';

interface ApprovalResponse {
  status: FederationSummaryApprovalStatus;
  approved_at?: string | null;
  can_manage: boolean;
}

interface FederationSummaryApprovalProps {
  apiUrl: string;
  sessionId: string;
  headers: HeadersInit;
  summaryVersion: string;
}

function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  return `Unable to update Customer-Ops approval (${status}).`;
}

function approvedAtLabel(value?: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

/**
 * Explicit human gate for the response-minimized Customer-Ops summary signal.
 * The server remains the authority for org/session permissions and stale digest
 * handling; this component only exposes that state where a meeting owner can
 * act on it.
 */
export function FederationSummaryApproval({
  apiUrl,
  sessionId,
  headers,
  summaryVersion,
}: FederationSummaryApprovalProps) {
  const [approval, setApproval] = useState<ApprovalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const headersRef = useRef(headers);
  headersRef.current = headers;

  const endpoint = `${apiUrl}/api/simple/recording-sessions/${sessionId}/federation-summary-approval`;

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(endpoint, { headers: headersRef.current, signal });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      if (!body || !['approved', 'unapproved', 'stale', 'unavailable'].includes(body.status)) {
        throw new Error('Customer-Ops approval returned an invalid status.');
      }
      if (signal?.aborted) return;
      setApproval(body as ApprovalResponse);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(cause instanceof Error ? cause.message : 'Unable to load Customer-Ops approval.');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [endpoint, summaryVersion]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const update = async (method: 'PUT' | 'DELETE') => {
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(endpoint, { method, headers: headersRef.current });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      setApproval(body as ApprovalResponse);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update Customer-Ops approval.');
    } finally {
      setSaving(false);
    }
  };

  const status = approval?.status;
  return (
    <section className="mb-5 rounded-lg border border-teal-200 bg-teal-50 p-4" aria-label="Customer-Ops summary sharing">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-teal-950">Customer-Ops summary sharing</h3>
          {loading ? (
            <p className="mt-1 text-xs text-teal-800">Checking approval status…</p>
          ) : status === 'approved' ? (
            <p className="mt-1 text-xs text-teal-800">
              Approved for Customer-Ops{approvedAtLabel(approval?.approved_at) ? ` on ${approvedAtLabel(approval?.approved_at)}.` : '.'}
            </p>
          ) : status === 'stale' ? (
            <p className="mt-1 text-xs text-amber-800">
              The summary changed after approval. Customer-Ops is not receiving it until you re-approve it.
            </p>
          ) : status === 'unavailable' ? (
            <p className="mt-1 text-xs text-slate-700">
              No privacy-safe summary is available to approve yet.
            </p>
          ) : (
            <p className="mt-1 text-xs text-teal-800">
              This summary is not shared with Customer-Ops until a meeting editor approves it.
            </p>
          )}
        </div>
        {!loading && approval?.can_manage && status === 'approved' ? (
          <button
            type="button"
            onClick={() => void update('DELETE')}
            disabled={saving}
            className="rounded-md border border-teal-300 bg-white px-3 py-1.5 text-xs font-medium text-teal-900 hover:bg-teal-100 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {saving ? 'Revoking…' : 'Revoke sharing'}
          </button>
        ) : !loading && approval?.can_manage && status !== 'unavailable' ? (
          <button
            type="button"
            onClick={() => void update('PUT')}
            disabled={saving}
            className="rounded-md bg-teal-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-teal-800 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {saving ? 'Saving…' : status === 'stale' ? 'Re-approve for Customer-Ops' : 'Approve for Customer-Ops'}
          </button>
        ) : null}
      </div>
      {error && (
        <div className="mt-3 flex flex-wrap items-center gap-2" role="alert">
          <p className="text-xs text-red-700">{error}</p>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading || saving}
            className="text-xs font-medium text-teal-800 underline disabled:opacity-60"
          >
            Retry status
          </button>
        </div>
      )}
    </section>
  );
}
