import { render, screen, act } from '@testing-library/react';
import { vi } from 'vitest';
import React from 'react';
import { AuthProvider, useAuth } from '../contexts/AuthContext';

// A test component that exposes AuthContext values
const AuthConsumer: React.FC = () => {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="is-authenticated">{String(auth.isAuthenticated)}</span>
      <span data-testid="has-login">{typeof auth.login === 'function' ? 'true' : 'false'}</span>
      <span data-testid="has-logout">{typeof auth.logout === 'function' ? 'true' : 'false'}</span>
      <span data-testid="is-loading">{String(auth.isLoading)}</span>
    </div>
  );
};

describe('AuthContext', () => {
  beforeEach(() => {
    // Mock localStorage
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => store[key] || null),
      setItem: vi.fn((key: string, value: string) => { store[key] = value; }),
      removeItem: vi.fn((key: string) => { delete store[key]; }),
      clear: vi.fn(() => { Object.keys(store).forEach(k => delete store[k]); }),
    });
    // Mock fetch for auth check
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'Not authenticated' }),
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('provides isAuthenticated', async () => {
    await act(async () => {
      render(
        <AuthProvider>
          <AuthConsumer />
        </AuthProvider>
      );
    });

    // Without a token in localStorage, user is not authenticated
    expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
  });

  it('provides login function', async () => {
    await act(async () => {
      render(
        <AuthProvider>
          <AuthConsumer />
        </AuthProvider>
      );
    });

    expect(screen.getByTestId('has-login').textContent).toBe('true');
  });
});
