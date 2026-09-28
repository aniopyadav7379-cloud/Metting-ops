import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, AlertCircle } from 'lucide-react';
import { config } from '../config';

interface RedeemResponse {
  valid: boolean;
  session_id?: string;
  session_db_id?: number;
  access_level?: 'read' | 'comment' | 'edit';
  reason?: string;
  bound_user_id?: number;
}

/**
 * Landing page for magic-link share invitations.
 *
 * Flow: oauth2-proxy ensures the user is already authenticated by the
 * time this component mounts (the route is gated like everything else
 * in the app, so anonymous visitors get bounced to Keycloak first).
 * We then call POST /api/simple/recording-sessions/permissions/redeem,
 * which binds the current user to the collaborator row for the token
 * and returns the session_id to navigate to.
 *
 * Failure modes are surfaced inline rather than redirecting to a
 * generic error: invalid token, expired, revoked, wrong-email, or the
 * session has been deleted.
 */
export const SharedSessionRedirect: React.FC = () => {
  const navigate = useNavigate();
  const [token] = useState(() => {
    try {
      const pending = window.sessionStorage.getItem(
        'meetingops.pendingInvitationSecret',
      );
      if (pending) return pending;
    } catch {
      // Storage can be disabled; legacy direct links still get a chance below.
    }
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    if (fragment.get('token')) return fragment.get('token') || '';
    // Compatibility for v1 links. New links always use the fragment so the
    // secret is not sent in HTTP request lines.
    return new URLSearchParams(window.location.search).get('token') || '';
  });
  const [status, setStatus] = useState<'pending' | 'ok' | 'error'>('pending');
  const [reason, setReason] = useState<string>('');

  useEffect(() => {
    if (!token) {
      setStatus('error');
      setReason('Missing share token.');
      return;
    }
    try {
      window.sessionStorage.removeItem('meetingops.pendingInvitationSecret');
    } catch {
      // The in-memory value is enough for this one redemption attempt.
    }
    // Remove the secret from browser history before making any API request.
    window.history.replaceState({}, '', window.location.pathname);
    (async () => {
      try {
        const res = await fetch(
          `${config.apiBaseUrl}/api/simple/recording-sessions/permissions/redeem`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token }),
          },
        );
        const data: RedeemResponse = await res.json();
        if (res.ok && data.valid && data.session_id) {
          setStatus('ok');
          navigate(`/sessions/${data.session_id}`, { replace: true });
        } else {
          setStatus('error');
          setReason(data.reason || `Could not open the shared meeting (HTTP ${res.status}).`);
        }
      } catch {
        setStatus('error');
        setReason('Network error while validating the share link.');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  return (
    <div className="min-h-screen bg-gray-950 text-white flex items-center justify-center px-4">
      <div className="w-full max-w-md rounded-lg border border-gray-800 bg-gray-900 p-6 shadow-xl">
        {status === 'pending' && (
          <div className="flex items-center gap-3 text-gray-200">
            <RefreshCw className="h-5 w-5 animate-spin text-purple-400" />
            <div>
              <h2 className="text-base font-semibold">Opening the shared meeting…</h2>
              <p className="mt-1 text-xs text-gray-500">
                Verifying your invitation and routing you to the recording.
              </p>
            </div>
          </div>
        )}
        {status === 'error' && (
          <div>
            <div className="flex items-start gap-2 text-amber-300">
              <AlertCircle className="h-5 w-5 flex-shrink-0 mt-0.5" />
              <div>
                <h2 className="text-base font-semibold text-white">
                  This share link can't be opened
                </h2>
                <p className="mt-1 text-sm text-gray-300">{reason}</p>
              </div>
            </div>
            <p className="mt-4 text-xs text-gray-500">
              If you believe this is a mistake, ask the person who shared the meeting
              to send you a fresh invitation. They can do this from the Share button
              on the meeting page.
            </p>
            <button
              onClick={() => navigate('/sessions', { replace: true })}
              className="mt-5 inline-flex items-center rounded bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700"
            >
              Go to your sessions
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default SharedSessionRedirect;
