import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import Landing from '../pages/Landing';

describe('Landing', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(),
    );
    // Reset hash between tests so the redirect assertion is reliable.
    window.location.hash = '';
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.location.hash = '';
  });

  it('renders hero copy, the key sections, and the invite form', () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );

    // Hero — product-page rework (2026-06-22): the category claim is now
    // "the open standard for conversation intelligence."
    expect(
      screen.getByRole('heading', {
        level: 1,
        name: /the open standard for conversation intelligence\./i,
      }),
    ).toBeInTheDocument();

    // Section signals from the 2026 product-page rework: how-it-works,
    // the privacy/moat contrast, and the pricing promise.
    expect(
      screen.getByText(/from mic to memory in three steps/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/your audio stays on your device/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/pay only for the heavy lifting/i),
    ).toBeInTheDocument();

    // Invite funnel — email capture + the "Continue" full-path CTA to /signup.
    expect(screen.getByLabelText(/email address/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /continue/i }),
    ).toBeInTheDocument();

    // Sign-in escape hatch (now surfaced in the nav + footer).
    expect(
      screen.getAllByRole('link', { name: /^sign in$/i }).length,
    ).toBeGreaterThan(0);

    // Footer attribution
    expect(
      screen.getByText(
        /built by magic unicorn unconventional technology & stuff inc\./i,
      ),
    ).toBeInTheDocument();
  });

  it('submits the invite request (fire-and-forget) and redirects to /signup with the email', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );

    const input = screen.getByLabelText(/email address/i);
    fireEvent.change(input, { target: { value: 'aaron@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/api/landing/invite-request');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({
      email: 'aaron@example.com',
    });

    // Full-path CTA: hash router navigates to /signup?email=...
    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#\/signup\?email=aaron%40example\.com$/);
    });
  });

  it('rejects an invalid email before hitting the network', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );

    const input = screen.getByLabelText(/email address/i);
    fireEvent.change(input, { target: { value: 'not-an-email' } });
    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/please enter a valid email address/i),
      ).toBeInTheDocument();
    });
    expect(fetchMock).not.toHaveBeenCalled();
    // No redirect on validation failure.
    expect(window.location.hash).toBe('');
  });

  it('redirects to /signup even when the invite-request POST fails (fire-and-forget)', async () => {
    // Server is down / rate-limited / 500'd — the redirect still
    // happens because v3.21.0 makes the invite log fire-and-forget.
    const fetchMock = vi.fn().mockRejectedValue(new Error('network down'));
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText(/email address/i), {
      target: { value: 'flood@example.com' },
    });
    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    await waitFor(() => {
      expect(window.location.hash).toMatch(/^#\/signup\?email=flood%40example\.com$/);
    });
  });
});
