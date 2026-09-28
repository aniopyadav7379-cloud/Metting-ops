import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';

import { SessionPermissionsModal } from '../components/SessionPermissionsModal';
import { AuthProvider } from '../contexts/AuthContext';
import { OrgProvider } from '../contexts/OrgContext';

const ORG = {
  id: 1,
  name: 'Magic Unicorn',
  slug: 'magic-unicorn',
  role: 'admin',
  is_active: true,
};

const ME = {
  id: 1,
  email: 'manager@example.com',
  username: 'manager',
  is_active: true,
  is_verified: true,
  is_superuser: false,
  created_at: '2026-07-24T00:00:00Z',
  organizations: [ORG],
  active_organization: ORG,
};

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => headers[name] || headers[name.toLowerCase()] || null,
    },
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as Response;
}

describe('SessionPermissionsModal', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows a fresh secret once while list data stays secret-free and resend is explicit', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const collaborators: Array<{
      id: number;
      email: string;
      access_level: 'read';
      accepted: boolean;
      delivery_state: 'sent';
      delivery_attempt_count: number;
    }> = [];
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) =>
        key === 'access_token'
          ? 'test-token'
          : key === 'meetingops.activeOrg'
            ? 'magic-unicorn'
            : null,
      ),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
    });
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        calls.push({ url, init });
        if (url.endsWith('/api/auth/me')) return jsonResponse(ME);
        if (
          url.endsWith('/permissions/collaborators') &&
          init?.method === 'POST'
        ) {
          const created = {
            id: 8,
            email: 'guest@example.com',
            access_level: 'read',
            accepted: false,
            delivery_state: 'sent',
            delivery_attempt_count: 1,
            created: true,
            delivered: true,
            invite_url_once: '/invite-bootstrap.html#token=fresh-secret-once',
          };
          collaborators.splice(0, collaborators.length, {
            id: created.id,
            email: created.email,
            access_level: 'read',
            accepted: false,
            delivery_state: 'sent',
            delivery_attempt_count: 1,
          });
          return jsonResponse(created);
        }
        if (url.endsWith('/permissions/collaborators/8/resend')) {
          return jsonResponse({
            collaborator: {
              ...collaborators[0],
              delivery_attempt_count: 2,
            },
            delivered: true,
            invite_url_once: '/invite-bootstrap.html#token=rotated-secret-once',
          });
        }
        if (url.endsWith('/permissions')) {
          return jsonResponse({
            session_id: '42',
            org_default: true,
            project_default: false,
            collaborators,
          });
        }
        return jsonResponse({}, 404);
      }),
    );

    await act(async () => {
      render(
        <MemoryRouter>
          <AuthProvider>
            <OrgProvider>
              <SessionPermissionsModal
                sessionId="42"
                isOpen
                onClose={vi.fn()}
                onEmailCopy={vi.fn()}
              />
            </OrgProvider>
          </AuthProvider>
        </MemoryRouter>,
      );
    });

    expect(await screen.findByText(/Invite for ongoing access/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Email static copy/i })).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('someone@example.com'), {
      target: { value: 'guest@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^Invite$/i }));

    expect(await screen.findByText('Fresh invitation link')).toBeInTheDocument();
    expect(screen.getByText(/shown only for this create or resend response/i)).toBeInTheDocument();
    expect(await screen.findByText('invitation sent')).toBeInTheDocument();
    expect(screen.queryByText('fresh-secret-once')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Copy fresh link/i }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        `${window.location.origin}/invite-bootstrap.html#token=fresh-secret-once`,
      );
    });

    fireEvent.click(screen.getByRole('button', { name: /^Resend$/i }));
    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url.endsWith('/permissions/collaborators/8/resend') &&
            call.init?.method === 'POST',
        ),
      ).toBe(true);
    });
    expect(screen.queryByText('rotated-secret-once')).not.toBeInTheDocument();
  });

  it('always clears loading and preserves the load error on an HTTP failure', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) =>
        key === 'access_token'
          ? 'test-token'
          : key === 'meetingops.activeOrg'
            ? 'magic-unicorn'
            : null,
      ),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/auth/me')) return jsonResponse(ME);
        if (url.endsWith('/permissions')) {
          return jsonResponse({ detail: 'permission service unavailable' }, 503);
        }
        return jsonResponse({}, 404);
      }),
    );

    await act(async () => {
      render(
        <MemoryRouter>
          <AuthProvider>
            <OrgProvider>
              <SessionPermissionsModal
                sessionId="42"
                isOpen
                onClose={vi.fn()}
              />
            </OrgProvider>
          </AuthProvider>
        </MemoryRouter>,
      );
    });

    expect(
      await screen.findByText(/Failed to load permissions \(HTTP 503\)/i),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText('Loading permissions…')).not.toBeInTheDocument();
    });
  });
});
