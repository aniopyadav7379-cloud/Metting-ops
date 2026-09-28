import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { uploadWebSocketUrl } from '../uploads';

// Regression: the upload-status WebSocket must carry the JWT as ?token= — the
// browser WS API can't set an Authorization header, and the backend handshake
// auth (auth/ws_auth.py) rejects tokenless sockets (403/1008 close), which made
// the progress toast hang on "processing" until a manual refresh.

function mockLocalStorage() {
  const store: Record<string, string> = {};
  return {
    getItem: (k: string) => (k in store ? store[k] : null),
    setItem: (k: string, v: string) => { store[k] = String(v); },
    removeItem: (k: string) => { delete store[k]; },
    clear: () => Object.keys(store).forEach((k) => delete store[k]),
    key: () => null,
    length: 0,
  };
}

describe('uploadWebSocketUrl', () => {
  beforeEach(() => vi.stubGlobal('localStorage', mockLocalStorage()));
  afterEach(() => vi.unstubAllGlobals());

  it('appends the JWT as ?token= so the WS handshake auth passes', () => {
    localStorage.setItem('access_token', 'jwt-abc.def.ghi');
    const url = uploadWebSocketUrl('job-123', 'magic-unicorn');
    expect(url).toMatch(/^wss?:\/\//);
    expect(url).toContain('/ws/uploads/job-123');
    expect(url).toContain('org=magic-unicorn');
    expect(url).toContain('token=jwt-abc.def.ghi');
  });

  it('omits token when not logged in but still includes org', () => {
    const url = uploadWebSocketUrl('job-9', 'acme');
    expect(url).toContain('/ws/uploads/job-9');
    expect(url).toContain('org=acme');
    expect(url).not.toContain('token=');
  });

  it('works with no org', () => {
    localStorage.setItem('access_token', 'tok');
    const url = uploadWebSocketUrl('j');
    expect(url).toContain('/ws/uploads/j');
    expect(url).toContain('token=tok');
    expect(url).not.toContain('org=');
  });
});
