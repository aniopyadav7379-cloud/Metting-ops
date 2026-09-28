import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

import PersonalAccessTokens from '../components/settings/PersonalAccessTokens';

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null),
    },
    json: async () => body,
  } as Response;
}

describe('PersonalAccessTokens', () => {
  let tokens: any[];
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    tokens = [
      {
        id: 1,
        name: 'Claude Desktop',
        token_prefix: 'mops_pat_ABC',
        last_used_at: null,
        created_at: '2026-05-28T00:00:00Z',
        revoked_at: null,
      },
    ];
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/auth/pats') && init?.method === 'POST') {
        const created = {
          id: 2,
          name: 'Cursor',
          token_prefix: 'mops_pat_DEF',
          plaintext: 'mops_pat_DEFGHIJKLMNOPQRSTUVWXYZ234567',
          last_used_at: null,
          created_at: '2026-05-28T00:01:00Z',
          revoked_at: null,
        };
        tokens = [created, ...tokens];
        return jsonResponse(created, 201);
      }
      if (url.includes('/api/auth/pats/1') && init?.method === 'DELETE') {
        tokens = tokens.map((token) =>
          token.id === 1 ? { ...token, revoked_at: '2026-05-28T00:02:00Z' } : token
        );
        return jsonResponse({}, 204);
      }
      if (url.includes('/api/auth/pats')) {
        return jsonResponse(tokens.map(({ plaintext, ...token }) => token));
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('navigator', {
      clipboard: {
        writeText: vi.fn(async () => undefined),
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders existing tokens without plaintext', async () => {
    render(<PersonalAccessTokens />);

    expect(await screen.findByText('Claude Desktop')).toBeInTheDocument();
    expect(screen.getByText('mops_pat_ABC...')).toBeInTheDocument();
    expect(screen.queryByText(/DEFGHIJKLMNOP/)).not.toBeInTheDocument();
  });

  it('creates a token and shows the plaintext once modal', async () => {
    render(<PersonalAccessTokens />);
    await screen.findByText('Claude Desktop');

    fireEvent.click(screen.getByRole('button', { name: /New Token/i }));
    fireEvent.change(screen.getByLabelText(/Token name/i), {
      target: { value: 'Cursor' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^Create$/i }));

    expect(await screen.findByText(/Copy this token now/i)).toBeInTheDocument();
    expect(screen.getByText('mops_pat_DEFGHIJKLMNOPQRSTUVWXYZ234567')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Copy$/i }));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        'mops_pat_DEFGHIJKLMNOPQRSTUVWXYZ234567'
      );
    });
  });

  it('creates an Omi integration token and surfaces the webhook URLs', async () => {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/auth/me')) {
        return jsonResponse({
          organizations: [{ slug: 'magic-unicorn', name: 'Magic Unicorn', role: 'admin', is_active: true }],
        });
      }
      if (url.includes('/api/auth/pats') && init?.method === 'POST') {
        const body = JSON.parse(init.body as string);
        expect(body.scope).toBe('omi.transcript.ingest');
        expect(body.organization_slug).toBe('magic-unicorn');
        expect(body.expires_in_days).toBe(90);
        const created = {
          id: 3,
          name: 'Omi bridge',
          token_prefix: 'mops_pat_OMI',
          plaintext: 'mops_pat_OMITOKEN1234567890',
          last_used_at: null,
          created_at: '2026-05-28T00:01:00Z',
          revoked_at: null,
          scope: 'omi.transcript.ingest',
          organization_id: 1,
          expires_at: '2026-08-28T00:01:00Z',
        };
        tokens = [created, ...tokens];
        return jsonResponse(created, 201);
      }
      if (url.includes('/api/auth/pats')) {
        return jsonResponse(tokens.map(({ plaintext, ...token }) => token));
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PersonalAccessTokens />);
    await screen.findByText('Claude Desktop');

    fireEvent.click(screen.getByRole('button', { name: /New Token/i }));
    fireEvent.change(screen.getByLabelText(/Token name/i), { target: { value: 'Omi bridge' } });
    fireEvent.change(screen.getByLabelText(/Purpose/i), {
      target: { value: 'omi' },
    });

    await screen.findByLabelText(/Organization/i);
    fireEvent.click(screen.getByRole('button', { name: /^Create$/i }));

    expect(await screen.findByText(/Set up in the Omi app/i)).toBeInTheDocument();
    expect(screen.getByText(/realtime-transcript/i)).toHaveTextContent(
      '/api/v1/integrations/omi/mops_pat_OMITOKEN1234567890/realtime-transcript'
    );
    expect(screen.getByText(/memory-created/i)).toHaveTextContent(
      '/api/v1/integrations/omi/mops_pat_OMITOKEN1234567890/memory-created'
    );
  });

  it('disables integration-token creation when the user has no admin org', async () => {
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/auth/me')) {
        return jsonResponse({ organizations: [{ slug: 'magic-unicorn', name: 'Magic Unicorn', role: 'member', is_active: true }] });
      }
      if (url.includes('/api/auth/pats')) {
        return jsonResponse(tokens.map(({ plaintext, ...token }) => token));
      }
      return jsonResponse({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PersonalAccessTokens />);
    await screen.findByText('Claude Desktop');

    fireEvent.click(screen.getByRole('button', { name: /New Token/i }));
    fireEvent.change(screen.getByLabelText(/Token name/i), { target: { value: 'Omi bridge' } });
    fireEvent.change(screen.getByLabelText(/Purpose/i), {
      target: { value: 'omi' },
    });

    expect(await screen.findByText(/need admin access/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Create$/i })).toBeDisabled();
  });

  it('revokes a token', async () => {
    render(<PersonalAccessTokens />);
    await screen.findByText('Claude Desktop');

    fireEvent.click(screen.getByRole('button', { name: /Revoke Claude Desktop/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/auth/pats/1'),
        expect.objectContaining({ method: 'DELETE' })
      );
    });
    expect(await screen.findByText('Revoked')).toBeInTheDocument();
  });
});
