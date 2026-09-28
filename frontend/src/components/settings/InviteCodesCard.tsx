import { useEffect, useState } from 'react';
import { Check, CircleCheck, Clipboard, RefreshCw, Ticket, XCircle } from 'lucide-react';
import { config } from '../../config';
import { showToast } from '../Toast';

/**
 * "Invite codes" panel — Phase 1 (display + share only).
 *
 * Lists the codes the current user has been minted (GET
 * /api/invite-codes/mine) with a copy-to-clipboard button and a status
 * badge. There is intentionally NO user-facing minting UI in Phase 1 —
 * admins mint codes for seed users; Phase 2 will let invitees mint their
 * own. The fetch goes through the global fetch interceptor, which injects
 * the bearer token for same-origin /api calls (same pattern as
 * PersonalAccessTokens), so no manual Authorization header is needed.
 */
interface InviteCode {
  code: string;
  is_active: boolean;
  redeemed: boolean;
  redeemed_by_email: string | null;
  redeemed_at: string | null;
  created_at: string;
}

type CodeStatus = 'available' | 'redeemed' | 'inactive';

function statusOf(code: InviteCode): CodeStatus {
  if (code.redeemed) return 'redeemed';
  if (!code.is_active) return 'inactive';
  return 'available';
}

function StatusBadge({ code }: { code: InviteCode }) {
  const status = statusOf(code);
  if (status === 'available') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2.5 py-1 text-xs font-medium text-emerald-300">
        <CircleCheck className="h-3.5 w-3.5" />
        Available
      </span>
    );
  }
  if (status === 'redeemed') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-zinc-700/50 px-2.5 py-1 text-xs font-medium text-zinc-300">
        <Check className="h-3.5 w-3.5" />
        Redeemed{code.redeemed_by_email ? ` by ${code.redeemed_by_email}` : ''}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-2.5 py-1 text-xs font-medium text-red-300">
      <XCircle className="h-3.5 w-3.5" />
      Inactive
    </span>
  );
}

export default function InviteCodesCard() {
  const [codes, setCodes] = useState<InviteCode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copiedCode, setCopiedCode] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${config.apiBaseUrl}/api/invite-codes/mine`, {
        headers: { Accept: 'application/json' },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCodes(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(`Failed to load invite codes: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const copyCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopiedCode(code);
      setTimeout(() => setCopiedCode((c) => (c === code ? null : c)), 2000);
      showToast.success('Invite code copied to clipboard.');
    } catch {
      showToast.error('Could not copy the code. Copy it manually.');
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm text-zinc-400">
          Share these codes to invite people to Meeting-Ops. Redeeming a code
          upgrades the new account&rsquo;s personal workspace to Pro. Each code
          works once.
        </p>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-950/30 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="overflow-hidden rounded-lg border border-zinc-800">
        <div className="grid grid-cols-12 gap-3 border-b border-zinc-800 bg-zinc-950/70 px-4 py-3 text-xs uppercase tracking-wide text-zinc-500">
          <div className="col-span-6">Code</div>
          <div className="col-span-5">Status</div>
          <div className="col-span-1 text-right">Copy</div>
        </div>
        {loading ? (
          <div className="flex items-center gap-2 px-4 py-6 text-sm text-zinc-400">
            <RefreshCw className="h-4 w-4 animate-spin" />
            Loading invite codes...
          </div>
        ) : codes.length === 0 ? (
          <div className="px-4 py-8 text-sm text-zinc-500">No invite codes yet.</div>
        ) : (
          codes.map((code) => (
            <div
              key={code.code}
              className="grid grid-cols-12 items-center gap-3 border-b border-zinc-800 px-4 py-3 text-sm last:border-b-0"
            >
              <div className="col-span-6 flex items-center gap-2 text-zinc-100">
                <Ticket className="h-4 w-4 shrink-0 text-fuchsia-300" />
                <span
                  className={`font-mono break-all ${
                    statusOf(code) === 'available' ? 'text-zinc-100' : 'text-zinc-500'
                  }`}
                >
                  {code.code}
                </span>
              </div>
              <div className="col-span-5">
                <StatusBadge code={code} />
              </div>
              <div className="col-span-1 text-right">
                <button
                  type="button"
                  aria-label={`Copy invite code ${code.code}`}
                  onClick={() => copyCode(code.code)}
                  className="inline-flex rounded-md p-2 text-zinc-400 hover:bg-zinc-800 hover:text-fuchsia-300"
                >
                  {copiedCode === code.code ? (
                    <Check className="h-4 w-4 text-emerald-300" />
                  ) : (
                    <Clipboard className="h-4 w-4" />
                  )}
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
