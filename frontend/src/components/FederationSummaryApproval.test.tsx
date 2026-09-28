import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FederationSummaryApproval } from './FederationSummaryApproval';

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function renderApproval() {
  return render(
    <FederationSummaryApproval
      apiUrl="http://meeting-ops.test"
      sessionId="session-1"
      headers={{ Authorization: 'Bearer test' }}
      summaryVersion="summary-v1"
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('FederationSummaryApproval', () => {
  it('loads unapproved state and approves it for Customer-Ops', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: 'unapproved', can_manage: true }))
      .mockResolvedValueOnce(response({ status: 'approved', approved_at: '2026-07-24T12:00:00Z', can_manage: true }));
    vi.stubGlobal('fetch', fetchMock);

    renderApproval();
    expect(await screen.findByText(/not shared with Customer-Ops/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve for Customer-Ops' }));

    await waitFor(() => expect(screen.getByText(/Approved for Customer-Ops/i)).toBeInTheDocument());
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PUT' });
  });

  it('explains stale state and offers an explicit re-approval', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: 'stale', can_manage: true }))
      .mockResolvedValueOnce(response({ status: 'approved', approved_at: '2026-07-24T12:00:00Z', can_manage: true }));
    vi.stubGlobal('fetch', fetchMock);

    renderApproval();
    expect(await screen.findByText(/changed after approval/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Re-approve for Customer-Ops' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Revoke sharing' })).toBeInTheDocument());
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PUT' });
  });

  it('renders unavailable and backend error states without allowing approval', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ status: 'unavailable', can_manage: false })));
    renderApproval();
    expect(await screen.findByText(/No privacy-safe summary/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Approve for Customer-Ops/i })).not.toBeInTheDocument();

    vi.unstubAllGlobals();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ detail: 'forbidden' }, 403)));
    renderApproval();
    expect(await screen.findByRole('alert')).toHaveTextContent('forbidden');
  });

  it('shows state but hides management actions for a view-only user', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ status: 'stale', can_manage: false })));
    renderApproval();
    expect(await screen.findByText(/changed after approval/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve|revoke/i })).not.toBeInTheDocument();
  });

  it('reloads approval when the displayed summary changes in place', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: 'approved', approved_at: '2026-07-24T12:00:00Z', can_manage: true }))
      .mockResolvedValueOnce(response({ status: 'stale', can_manage: true }));
    vi.stubGlobal('fetch', fetchMock);
    const { rerender } = renderApproval();
    expect(await screen.findByRole('button', { name: 'Revoke sharing' })).toBeInTheDocument();

    rerender(
      <FederationSummaryApproval
        apiUrl="http://meeting-ops.test"
        sessionId="session-1"
        headers={{ Authorization: 'Bearer test' }}
        summaryVersion="summary-v2"
      />,
    );

    expect(await screen.findByRole('button', { name: 'Re-approve for Customer-Ops' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
