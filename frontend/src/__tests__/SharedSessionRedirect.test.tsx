import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';

import { SharedSessionRedirect } from '../components/SharedSessionRedirect';

describe('SharedSessionRedirect', () => {
  afterEach(() => {
    window.history.replaceState({}, '', '/');
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('consumes the post-SSO session handoff and posts the secret in JSON', async () => {
    window.history.replaceState({}, '', '/shared/sessions');
    window.sessionStorage.setItem(
      'meetingops.pendingInvitationSecret',
      'fragment-secret-value-123456',
    );
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        valid: true,
        session_id: 'meeting-42',
        session_db_id: 42,
        access_level: 'read',
      }),
    } as Response));
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/shared/sessions']}>
        <Routes>
          <Route path="/shared/sessions" element={<SharedSessionRedirect />} />
          <Route path="/sessions/:id" element={<div>Meeting opened</div>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText('Meeting opened')).toBeInTheDocument();
    expect(window.location.hash).toBe('');
    expect(
      window.sessionStorage.getItem('meetingops.pendingInvitationSecret'),
    ).toBeNull();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/permissions/redeem'),
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ token: 'fragment-secret-value-123456' }),
        }),
      );
    });
  });

  it.each([
    ['invitation not found', 'invalid'],
    ['expired', 'expired'],
    ['revoked', 'revoked'],
  ])(
    'removes the same-tab secret after the terminal %s response',
    async (reason) => {
      const secret = `terminal-${reason}-secret-1234567890`;
      window.history.replaceState({}, '', '/shared/sessions');
      window.sessionStorage.setItem('meetingops.pendingInvitationSecret', secret);
      const fetchMock = vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ valid: false, reason }),
      } as Response));
      vi.stubGlobal('fetch', fetchMock);

      render(
        <MemoryRouter initialEntries={['/shared/sessions']}>
          <Routes>
            <Route path="/shared/sessions" element={<SharedSessionRedirect />} />
          </Routes>
        </MemoryRouter>,
      );

      expect(await screen.findByText(reason)).toBeInTheDocument();
      expect(
        window.sessionStorage.getItem('meetingops.pendingInvitationSecret'),
      ).toBeNull();
      expect(window.location.href).not.toContain(secret);
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringContaining('/permissions/redeem'),
          expect.objectContaining({
            method: 'POST',
            body: JSON.stringify({ token: secret }),
          }),
        );
      });
    },
  );
});
