import React, { useEffect, useState } from 'react';
import { X, UserPlus, Copy, Trash2, RefreshCw, Mail, Users, ExternalLink, AlertCircle } from 'lucide-react';
import { config } from '../config';
import { showConfirm } from '../utils/notifications';
import { useOrg } from '../contexts/OrgContext';

type AccessLevel = 'read' | 'comment' | 'edit';

interface CollaboratorUser {
  id: number;
  email: string;
  username?: string | null;
  full_name?: string | null;
}

interface Collaborator {
  id: number;
  user?: CollaboratorUser | null;
  email?: string | null;
  access_level: AccessLevel;
  expires_at?: string | null;
  accepted_at?: string | null;
  revoked_at?: string | null;
  created_at?: string | null;
  accepted: boolean;
  delivery_state: 'pending' | 'sent' | 'failed' | 'accepted' | 'revoked' | 'expired';
  delivery_attempt_count: number;
  last_delivery_attempt_at?: string | null;
  delivery_failure_reason?: string | null;
}

interface CollaboratorCreateResponse extends Collaborator {
  created: boolean;
  invite_url_once?: string | null;
  delivered?: boolean | null;
}

interface ResendResponse {
  collaborator: Collaborator;
  invite_url_once: string;
  delivered: boolean;
}

interface PermissionsResponse {
  session_id: string;
  org_default: boolean;
  project_default: boolean;
  collaborators: Collaborator[];
}

interface Props {
  sessionId: string | number;
  isOpen: boolean;
  onClose: () => void;
  onEmailCopy?: () => void;
}

const ACCESS_LEVELS: { id: AccessLevel; label: string; hint: string }[] = [
  { id: 'read', label: 'Can view', hint: 'See the transcript, summary, and audio.' },
  { id: 'comment', label: 'Can comment', hint: 'View plus add chat / comments.' },
  { id: 'edit', label: 'Can edit', hint: 'View plus rename speakers and tweak.' },
];

export const SessionPermissionsModal: React.FC<Props> = ({
  sessionId,
  isOpen,
  onClose,
  onEmailCopy,
}) => {
  const { activeOrganization } = useOrg();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [permissions, setPermissions] = useState<PermissionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draftEmail, setDraftEmail] = useState('');
  const [draftLevel, setDraftLevel] = useState<AccessLevel>('read');
  const [oneTimeInviteLink, setOneTimeInviteLink] = useState<string | null>(null);
  const [copiedInviteLink, setCopiedInviteLink] = useState(false);

  const orgHeaders = (): Record<string, string> => {
    const headers: Record<string, string> = {};
    if (activeOrganization?.slug) headers['X-MeetingOps-Org'] = activeOrganization.slug;
    return headers;
  };

  const loadPermissions = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}/permissions`,
        { headers: orgHeaders() },
      );
      if (!res.ok) {
        const txt = await res.text();
        setError(`Failed to load permissions (HTTP ${res.status}): ${txt.slice(0, 160)}`);
        return;
      }
      const data: PermissionsResponse = await res.json();
      setPermissions(data);
    } catch {
      setError('Network error loading permissions.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      setOneTimeInviteLink(null);
      setCopiedInviteLink(false);
      loadPermissions();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, sessionId, activeOrganization?.slug]);

  const inviteByEmail = async () => {
    const email = draftEmail.trim();
    if (!email) {
      setError('Enter an email to invite.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}/permissions/collaborators`,
        {
          method: 'POST',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, access_level: draftLevel }),
        },
      );
      if (res.ok) {
        const created: CollaboratorCreateResponse = await res.json();
        setDraftEmail('');
        setOneTimeInviteLink(created.invite_url_once || null);
        setCopiedInviteLink(false);
        await loadPermissions();
      } else {
        const txt = await res.text();
        setError(`Invite failed (HTTP ${res.status}): ${txt.slice(0, 160)}`);
      }
    } catch {
      setError('Network error sending invite.');
    } finally {
      setSaving(false);
    }
  };

  const resendInvitation = async (collaboratorId: number) => {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}/permissions/collaborators/${collaboratorId}/resend`,
        { method: 'POST', headers: orgHeaders() },
      );
      if (res.ok) {
        const resent: ResendResponse = await res.json();
        setOneTimeInviteLink(resent.invite_url_once);
        setCopiedInviteLink(false);
        await loadPermissions();
      } else if (res.status === 429) {
        const retryAfter = res.headers.get('Retry-After');
        setError(`Invitation was attempted recently. Try again in ${retryAfter || 'a few'} seconds.`);
      } else {
        const txt = await res.text();
        setError(`Resend failed (HTTP ${res.status}): ${txt.slice(0, 160)}`);
      }
    } catch {
      setError('Network error resending invitation.');
    } finally {
      setSaving(false);
    }
  };

  const updateAccess = async (collaboratorId: number, level: AccessLevel) => {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}/permissions/collaborators/${collaboratorId}`,
        {
          method: 'PATCH',
          headers: { ...orgHeaders(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ access_level: level }),
        },
      );
      if (res.ok) {
        await loadPermissions();
      } else {
        const txt = await res.text();
        setError(`Update failed (HTTP ${res.status}): ${txt.slice(0, 160)}`);
      }
    } catch {
      setError('Network error updating access.');
    } finally {
      setSaving(false);
    }
  };

  const revokeCollaborator = async (collaboratorId: number) => {
    if (!(await showConfirm('Revoke this person’s access to the meeting?', {
      title: 'Revoke access', confirmLabel: 'Revoke',
    }))) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `${config.apiBaseUrl}/api/simple/recording-sessions/${sessionId}/permissions/collaborators/${collaboratorId}`,
        { method: 'DELETE', headers: orgHeaders() },
      );
      if (res.ok || res.status === 204) {
        await loadPermissions();
      } else {
        const txt = await res.text();
        setError(`Revoke failed (HTTP ${res.status}): ${txt.slice(0, 160)}`);
      }
    } catch {
      setError('Network error revoking access.');
    } finally {
      setSaving(false);
    }
  };

  const copyInviteLink = () => {
    if (!oneTimeInviteLink) return;
    // Backend may hand back a relative path when MEETING_OPS_PUBLIC_URL is unset.
    const raw = oneTimeInviteLink;
    const url = raw.startsWith('http') ? raw : `${window.location.origin}${raw}`;
    navigator.clipboard.writeText(url)
      .then(() => {
        setCopiedInviteLink(true);
        setTimeout(() => setCopiedInviteLink(false), 2000);
      })
      .catch(() => { /* clipboard blocked — leave the chip unset */ });
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6">
      <div className="w-full max-w-2xl rounded-lg border border-gray-700 bg-gray-950 shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-800 px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-white">Share this meeting</h2>
            <p className="mt-0.5 text-xs text-gray-400">
              Invite people for ongoing access, or email a one-time copy.
            </p>
          </div>
          <button onClick={onClose} className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto px-5 py-4 space-y-5">
          {loading && (
            <div className="flex items-center gap-2 text-sm text-gray-400">
              <RefreshCw className="h-4 w-4 animate-spin" />
              Loading permissions…
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 rounded border border-red-700 bg-red-900/30 px-3 py-2 text-xs text-red-300">
              <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {permissions && (
            <>
              {/* Defaults summary */}
              <div className="space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-gray-400">Default access</p>
                <div className="space-y-1.5">
                  {permissions.org_default && (
                    <div className="flex items-center gap-2 text-sm text-gray-200">
                      <Users className="h-4 w-4 text-cyan-400" />
                      Everyone in your org can view this meeting.
                    </div>
                  )}
                  {permissions.project_default && (
                    <div className="flex items-center gap-2 text-sm text-gray-200">
                      <Users className="h-4 w-4 text-emerald-400" />
                      Members of the linked project can view this meeting.
                    </div>
                  )}
                  {!permissions.org_default && !permissions.project_default && (
                    <p className="text-sm text-gray-500">
                      Only the people listed below can see this meeting.
                    </p>
                  )}
                </div>
              </div>

              {/* Add an ongoing-access collaborator */}
              <div className="rounded-lg border border-gray-800 bg-gray-900 p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-1">
                  Invite for ongoing access
                </p>
                <p className="mb-3 text-xs text-gray-500">
                  They can return to the live meeting record until you revoke access.
                  This is different from emailing a static PDF copy.
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <div className="relative flex-1 min-w-[200px]">
                    <Mail className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-500" />
                    <input
                      type="email"
                      value={draftEmail}
                      onChange={(e) => setDraftEmail(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter' && !saving) inviteByEmail(); }}
                      placeholder="someone@example.com"
                      disabled={saving}
                      className="w-full pl-9 pr-3 py-2 rounded border border-gray-700 bg-gray-950 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
                    />
                  </div>
                  <select
                    value={draftLevel}
                    onChange={(e) => setDraftLevel(e.target.value as AccessLevel)}
                    disabled={saving}
                    className="rounded border border-gray-700 bg-gray-950 px-2 py-2 text-sm text-white"
                  >
                    {ACCESS_LEVELS.map((l) => (
                      <option key={l.id} value={l.id}>{l.label}</option>
                    ))}
                  </select>
                  <button
                    onClick={inviteByEmail}
                    disabled={saving || !draftEmail.trim()}
                    className="rounded bg-cyan-600 px-3 py-2 text-sm font-medium text-white hover:bg-cyan-500 disabled:opacity-50 flex items-center gap-2"
                  >
                    {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />}
                    Invite
                  </button>
                </div>
                <p className="mt-2 text-xs text-gray-500">
                  If they already have an account on this instance they'll see the meeting on next sign-in.
                  Otherwise they get a single-use-visible magic link bound to this email address.
                </p>
              </div>

              {oneTimeInviteLink && (
                <div className="rounded-lg border border-cyan-700/70 bg-cyan-950/30 p-3">
                  <p className="text-sm font-medium text-cyan-100">Fresh invitation link</p>
                  <p className="mt-1 text-xs text-cyan-200/70">
                    This link is shown only for this create or resend response. Copy it
                    now if you need a manual delivery; it will not appear in the people list.
                  </p>
                  <button
                    type="button"
                    onClick={copyInviteLink}
                    className="mt-3 inline-flex items-center gap-1.5 rounded border border-cyan-700 px-2.5 py-1.5 text-xs text-cyan-100 hover:bg-cyan-900/40"
                  >
                    <Copy className="h-3.5 w-3.5" />
                    {copiedInviteLink ? 'Copied!' : 'Copy fresh link'}
                  </button>
                </div>
              )}

              {/* Collaborator list */}
              {permissions.collaborators.length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-gray-400">People with access</p>
                  <div className="space-y-2">
                    {permissions.collaborators.map((c) => {
                      const displayName =
                        c.user?.full_name ||
                        c.user?.username ||
                        c.user?.email ||
                        c.email ||
                        '(unknown)';
                      const canResend =
                        Boolean(c.email) &&
                        ['pending', 'sent', 'failed'].includes(c.delivery_state);
                      const isInactive = ['revoked', 'expired'].includes(c.delivery_state);
                      const stateLabel = c.delivery_state === 'accepted'
                        ? 'access accepted'
                        : c.delivery_state === 'sent'
                          ? 'invitation sent'
                          : c.delivery_state === 'failed'
                            ? 'delivery failed'
                            : c.delivery_state;
                      return (
                        <div
                          key={c.id}
                          className="flex flex-wrap items-center gap-3 rounded-md border border-gray-800 bg-gray-900 px-3 py-2 text-sm"
                        >
                          <div className="flex-1 min-w-[180px]">
                            <div className="font-medium text-white truncate">{displayName}</div>
                            {c.user?.email && c.user.email !== displayName && (
                              <div className="text-xs text-gray-500 truncate">{c.user.email}</div>
                            )}
                            <div className={`mt-0.5 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                              c.delivery_state === 'failed'
                                ? 'bg-red-500/15 text-red-300'
                                : c.delivery_state === 'accepted'
                                  ? 'bg-emerald-500/15 text-emerald-300'
                                  : isInactive
                                    ? 'bg-gray-700/50 text-gray-400'
                                    : 'bg-amber-500/15 text-amber-300'
                            }`}>
                              {stateLabel}
                            </div>
                            {c.delivery_state === 'failed' && c.delivery_failure_reason && (
                              <div className="mt-1 text-[10px] text-red-300/80">
                                {c.delivery_failure_reason.replaceAll('_', ' ')}
                              </div>
                            )}
                          </div>
                          <select
                            value={c.access_level}
                            onChange={(e) => updateAccess(c.id, e.target.value as AccessLevel)}
                            disabled={saving || isInactive}
                            className="rounded border border-gray-700 bg-gray-950 px-2 py-1 text-xs text-white"
                          >
                            {ACCESS_LEVELS.map((l) => (
                              <option key={l.id} value={l.id}>{l.label}</option>
                            ))}
                          </select>
                          {canResend && (
                            <button
                              onClick={() => resendInvitation(c.id)}
                              disabled={saving}
                              className="flex items-center gap-1 rounded border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:bg-gray-800 disabled:opacity-50"
                              title="Rotate and resend invitation"
                            >
                              <RefreshCw className="h-3 w-3" />
                              Resend
                            </button>
                          )}
                          {!isInactive && (
                            <button
                              onClick={() => revokeCollaborator(c.id)}
                              disabled={saving}
                              className="flex items-center gap-1 rounded border border-red-800/50 px-2 py-1 text-xs text-red-300 hover:bg-red-900/30 disabled:opacity-50"
                              title="Revoke access immediately"
                            >
                              <Trash2 className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {permissions.collaborators.length === 0 && (
                <p className="text-xs text-gray-500">
                  No one has been explicitly invited yet. Org defaults still apply (see above).
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-gray-800 px-5 py-3">
          <a
            href={`${window.location.origin}/settings/members`}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1"
          >
            <ExternalLink className="h-3 w-3" />
            Manage org-level membership
          </a>
          <div className="flex items-center gap-2">
            {onEmailCopy && (
              <button
                type="button"
                onClick={onEmailCopy}
                title="Send a static PDF copy without granting ongoing access"
                className="inline-flex items-center gap-2 rounded-md bg-cyan-600 px-4 py-2 text-sm font-medium text-white hover:bg-cyan-500"
              >
                <Mail className="h-4 w-4" />
                Email static copy
              </button>
            )}
            <button
              onClick={onClose}
              className="rounded-md bg-gray-800 px-4 py-2 text-sm text-gray-200 hover:bg-gray-700"
            >
              Done
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SessionPermissionsModal;
