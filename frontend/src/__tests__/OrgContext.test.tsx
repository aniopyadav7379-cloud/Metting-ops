import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

import { AuthProvider } from '../contexts/AuthContext';
import { OrgProvider, useOrg } from '../contexts/OrgContext';

const OrgConsumer: React.FC = () => {
  const { activeOrganization, organizations, switchOrganization, isLoading } = useOrg();

  if (isLoading) {
    return <div data-testid="org-loading">loading</div>;
  }

  return (
    <div>
      <div data-testid="active-org">{activeOrganization?.slug ?? 'none'}</div>
      <div data-testid="org-count">{organizations.length}</div>
      <button type="button" onClick={() => switchOrganization('beta-team')}>
        Switch
      </button>
    </div>
  );
};

describe('OrgContext', () => {
  beforeEach(() => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => store[key] || null),
      setItem: vi.fn((key: string, value: string) => { store[key] = value; }),
      removeItem: vi.fn((key: string) => { delete store[key]; }),
      clear: vi.fn(() => { Object.keys(store).forEach((key) => delete store[key]); }),
    });

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: {
        get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null),
      },
      json: async () => ({
        id: 1,
        email: 'test@example.com',
        username: 'tester',
        is_active: true,
        is_verified: true,
        created_at: '2026-05-03T00:00:00Z',
        organizations: [
          { id: 1, name: 'Magic Unicorn', slug: 'magic-unicorn', role: 'admin', is_active: true },
          { id: 2, name: 'Beta Team', slug: 'beta-team', role: 'user', is_active: true },
        ],
        active_organization: { id: 1, name: 'Magic Unicorn', slug: 'magic-unicorn', role: 'admin', is_active: true },
      }),
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('falls back to the default org and switches to another org', async () => {
    await act(async () => {
      render(
        <AuthProvider>
          <OrgProvider>
            <OrgConsumer />
          </OrgProvider>
        </AuthProvider>
      );
    });

    await waitFor(() => expect(screen.getByTestId('org-count').textContent).toBe('2'));
    expect(screen.getByTestId('active-org').textContent).toBe('magic-unicorn');

    fireEvent.click(screen.getByText('Switch'));

    await waitFor(() => expect(screen.getByTestId('active-org').textContent).toBe('beta-team'));
    expect(localStorage.setItem).toHaveBeenCalledWith('meetingops.activeOrg', 'beta-team');
  });
});
