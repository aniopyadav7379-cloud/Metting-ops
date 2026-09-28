import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import RAGChat from '../pages/RAGChat';
import { AuthProvider } from '../contexts/AuthContext';
import { OrgProvider } from '../contexts/OrgContext';

const ORG_FIXTURE = {
  id: 1,
  name: 'Magic Unicorn',
  slug: 'magic-unicorn',
  role: 'admin',
  is_active: true,
};

const ME_PAYLOAD = {
  id: 1,
  email: 'test@example.com',
  username: 'tester',
  is_active: true,
  is_verified: true,
  created_at: '2026-05-03T00:00:00Z',
  organizations: [ORG_FIXTURE],
  active_organization: ORG_FIXTURE,
};

const LOCAL_AGENT = {
  id: 'meeting-rag',
  name: 'Meeting RAG',
  description: "Searches and synthesizes across this org's meetings",
  source: 'local',
  icon: '🎙️',
};

const BRIGADE_AGENT = {
  id: 'brigade:sql-analyst',
  name: 'SQL Analyst',
  description: 'Text-to-SQL & Database Query Specialist',
  source: 'brigade',
  icon: '🗄️',
};

const BRIGADE_AGENT_2 = {
  id: 'brigade:research-agent',
  name: 'Research Agent',
  description: 'Web research and synthesis',
  source: 'brigade',
  icon: '🔬',
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

function streamingResponse(events: string[], status = 200) {
  const encoder = new TextEncoder();
  let i = 0;
  const reader = {
    read: vi.fn(async () => {
      if (i >= events.length) {
        return { done: true, value: undefined };
      }
      const chunk = encoder.encode(events[i]);
      i += 1;
      return { done: false, value: chunk };
    }),
  };
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) =>
        name.toLowerCase() === 'content-type' ? 'text/event-stream' : null,
    },
    body: { getReader: () => reader },
  } as unknown as Response;
}

interface FetchCall {
  url: string;
  init?: RequestInit;
}

let fetchMock: ReturnType<typeof vi.fn>;
let fetchCalls: FetchCall[];

interface InstallOpts {
  agentsResponse?: { agents: any[]; warnings?: string[] } | null;
  agentsStatus?: number;
  agentsThrows?: boolean;
  streamEvents?: string[];
}

function installFetch(opts: InstallOpts = {}) {
  fetchCalls = [];
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    fetchCalls.push({ url, init });

    if (url.endsWith('/api/auth/me')) {
      return jsonResponse(ME_PAYLOAD);
    }
    if (url.includes('/api/agents/available')) {
      if (opts.agentsThrows) {
        throw new Error('network down');
      }
      const status = opts.agentsStatus ?? 200;
      const body = opts.agentsResponse ?? {
        agents: [LOCAL_AGENT, BRIGADE_AGENT, BRIGADE_AGENT_2],
        warnings: [],
      };
      return jsonResponse(body, status);
    }
    if (url.includes('/api/rag/chat/history')) {
      if (init?.method === 'DELETE') {
        return jsonResponse({ ok: true });
      }
      return jsonResponse([]);
    }
    if (url.includes('/api/agents/chat')) {
      const events = opts.streamEvents ?? ['data: {"done": true}\n\n'];
      return streamingResponse(events);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
}

function installLocalStorage() {
  const store: Record<string, string> = { access_token: 'test-token' };
  vi.stubGlobal('localStorage', {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      Object.keys(store).forEach((key) => delete store[key]);
    }),
  });
}

async function renderRAGChat() {
  await act(async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <OrgProvider>
            <RAGChat />
          </OrgProvider>
        </AuthProvider>
      </MemoryRouter>
    );
  });
  await waitFor(() =>
    expect(
      screen.getByPlaceholderText(/ask a question about your meetings/i)
    ).toBeInTheDocument()
  );
}

describe('AgentPicker', () => {
  beforeEach(() => {
    installLocalStorage();
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders local + brigade agents grouped by source', async () => {
    installFetch();
    await renderRAGChat();

    // Wait for the agents fetch to land + default selection to appear.
    await waitFor(() => {
      const calls = fetchCalls.filter((c) => c.url.includes('/api/agents/available'));
      expect(calls.length).toBeGreaterThanOrEqual(1);
    });

    // Open the picker.
    const pickerButton = await screen.findByTestId('agent-picker-button');
    fireEvent.click(pickerButton);

    const list = await screen.findByTestId('agent-picker-list');
    expect(list).toBeInTheDocument();

    // Group headers visible
    expect(screen.getByText(/Meeting Ops/i)).toBeInTheDocument();
    expect(screen.getByText(/Brigade Agents/i)).toBeInTheDocument();

    // Local + brigade agent options visible
    expect(screen.getByTestId('agent-option-meeting-rag')).toBeInTheDocument();
    expect(screen.getByTestId('agent-option-brigade:sql-analyst')).toBeInTheDocument();
    expect(screen.getByTestId('agent-option-brigade:research-agent')).toBeInTheDocument();
  });

  it('routes the chat request to the selected brigade agent', async () => {
    installFetch();
    await renderRAGChat();

    await waitFor(() => {
      expect(
        fetchCalls.find((c) => c.url.includes('/api/agents/available'))
      ).toBeTruthy();
    });

    // Switch to a Brigade agent
    fireEvent.click(await screen.findByTestId('agent-picker-button'));
    fireEvent.click(await screen.findByTestId('agent-option-brigade:sql-analyst'));

    // Send a message
    const input = screen.getByPlaceholderText(/ask a question about your meetings/i);
    fireEvent.change(input, { target: { value: 'count rows in users table' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });

    await waitFor(() => {
      const chatPost = fetchCalls.find(
        (c) => c.url.includes('/api/agents/chat') && c.init?.method === 'POST'
      );
      expect(chatPost).toBeTruthy();
      const body = JSON.parse(chatPost?.init?.body as string);
      expect(body.agent_id).toBe('brigade:sql-analyst');
      expect(body.stream).toBe(true);
      // Brigade selection should not send a scope object
      expect(body.scope).toBeUndefined();
    });
  });

  it('falls back to local-only when the agents endpoint errors', async () => {
    installFetch({ agentsResponse: null, agentsStatus: 500 });
    await renderRAGChat();

    await waitFor(() => {
      const calls = fetchCalls.filter((c) => c.url.includes('/api/agents/available'));
      expect(calls.length).toBeGreaterThanOrEqual(1);
    });

    // Warning rendered
    expect(await screen.findByTestId('agent-warnings')).toBeInTheDocument();

    // Open the picker — should only show the local fallback
    fireEvent.click(await screen.findByTestId('agent-picker-button'));
    expect(await screen.findByTestId('agent-option-meeting-rag')).toBeInTheDocument();
    expect(screen.queryByTestId('agent-option-brigade:sql-analyst')).not.toBeInTheDocument();
  });

  it('falls back to local-only when the agents fetch throws', async () => {
    installFetch({ agentsThrows: true });
    await renderRAGChat();

    await waitFor(() => {
      const calls = fetchCalls.filter((c) => c.url.includes('/api/agents/available'));
      expect(calls.length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(await screen.findByTestId('agent-picker-button'));
    expect(await screen.findByTestId('agent-option-meeting-rag')).toBeInTheDocument();
  });

  it('shows the "Powered by Brigade" badge when a brigade agent is selected', async () => {
    installFetch();
    await renderRAGChat();

    await waitFor(() => {
      expect(
        fetchCalls.find((c) => c.url.includes('/api/agents/available'))
      ).toBeTruthy();
    });

    fireEvent.click(await screen.findByTestId('agent-picker-button'));
    fireEvent.click(await screen.findByTestId('agent-option-brigade:sql-analyst'));

    // Badge appears in the picker button's selected label.
    await waitFor(() => {
      expect(screen.getByText(/Powered by Brigade/i)).toBeInTheDocument();
    });
  });
});
