import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';

import { EmailAttendeesModal } from '../components/EmailAttendeesModal';
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
  email: 'tester@example.com',
  username: 'tester',
  is_active: true,
  is_verified: true,
  is_superuser: false,
  created_at: '2026-07-24T00:00:00Z',
  organizations: [ORG],
  active_organization: ORG,
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) =>
        name.toLowerCase() === 'content-type' ? 'application/json' : null,
    },
    json: async () => body,
  } as Response;
}

describe('EmailAttendeesModal', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('joins speaker links to the session-scoped speaker library and sends the selected identity', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
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
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        calls.push({ url, init });
        if (url.endsWith('/api/auth/me')) return jsonResponse(ME);
        if (url.endsWith('/api/sessions/42/speaker-links')) {
          return jsonResponse([
            {
              id: 10,
              raw_label: 'SPEAKER_00',
              speaker_id: 7,
              speaker_name: 'Aaron Stransky',
            },
          ]);
        }
        if (url.includes('/api/speakers?session_id=42')) {
          return jsonResponse([
            {
              id: 7,
              display_name: 'Aaron Stransky',
              email: 'aaron@example.com',
            },
          ]);
        }
        if (url.endsWith('/api/simple/recording-sessions/42/email-attendees')) {
          return jsonResponse({ sent: 1, skipped: 0, failures: [] });
        }
        return jsonResponse({}, 404);
      }),
    );

    await act(async () => {
      render(
        <MemoryRouter>
          <AuthProvider>
            <OrgProvider>
              <EmailAttendeesModal
                sessionId="42"
                isOpen
                onClose={vi.fn()}
              />
            </OrgProvider>
          </AuthProvider>
        </MemoryRouter>,
      );
    });

    const recipient = await screen.findByRole('checkbox', {
      name: /Aaron Stransky.*aaron@example.com/i,
    });
    expect(recipient).toBeChecked();
    expect(
      screen.getByRole('checkbox', { name: /Static summary copy/i }),
    ).toBeChecked();
    expect(
      screen.getByRole('checkbox', { name: /Ongoing access link/i }),
    ).not.toBeChecked();
    expect(
      screen.getByText(/receive ongoing access only if you select/i),
    ).toBeInTheDocument();
    expect(
      calls.some((call) =>
        call.url.includes('/api/speakers?session_id=42'),
      ),
    ).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /^Send$/i }));

    await waitFor(() => {
      const sendCall = calls.find(
        (call) =>
          call.url.endsWith(
            '/api/simple/recording-sessions/42/email-attendees',
          ) && call.init?.method === 'POST',
      );
      expect(sendCall).toBeTruthy();
      expect(JSON.parse(sendCall?.init?.body as string)).toEqual({
        speaker_ids: [7],
        additional_recipients: [],
        include: ['summary_pdf'],
        brand_mode: 'default',
      });
    });
    expect(await screen.findByText(/Sent 1 email/i)).toBeInTheDocument();
  });
});
