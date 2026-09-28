import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { SpeakerLibrary } from "../pages/SpeakerLibrary";
import { AuthProvider } from "../contexts/AuthContext";
import { OrgProvider } from "../contexts/OrgContext";

const ADMIN_ORG = {
  id: 1,
  name: "Magic Unicorn",
  slug: "magic-unicorn",
  role: "admin",
  is_active: true,
};

const USER_ORG = { ...ADMIN_ORG, role: "user" };

function meWith(org: typeof ADMIN_ORG) {
  return {
    id: 1,
    email: "test@example.com",
    username: "tester",
    is_active: true,
    is_verified: true,
    is_superuser: false,
    created_at: "2026-05-03T00:00:00Z",
    organizations: [org],
    active_organization: org,
  };
}

const SPEAKER_FIXTURES = [
  {
    id: 7,
    organization_id: 1,
    display_name: "Aaron Stransky",
    email: "aaron@magicunicorn.tech",
    notes: null,
    embedding_dim: 192,
    embedding_model: "speechbrain/spkrec-ecapa-voxceleb",
    sample_count: 2,
    has_centroid: true,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-02T00:00:00Z",
  },
  {
    id: 8,
    organization_id: 1,
    display_name: "Shafen Khan",
    email: null,
    notes: "GFL co-founder",
    embedding_dim: null,
    embedding_model: null,
    sample_count: 0,
    has_centroid: false,
    created_at: "2026-05-02T00:00:00Z",
    updated_at: "2026-05-02T00:00:00Z",
  },
];

const SPEAKER_DETAIL = {
  ...SPEAKER_FIXTURES[0],
  voice_samples: [
    {
      id: 100,
      speaker_id: 7,
      source: "enrollment",
      source_session_id: null,
      embedding_dim: 192,
      embedding_model: "speechbrain/spkrec-ecapa-voxceleb",
      duration_seconds: 8.4,
      similarity_to_centroid: 0.92,
      has_audio: false,
      created_at: "2026-05-01T00:00:00Z",
    },
  ],
  session_count: 4,
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === "content-type" ? "application/json" : null),
    },
    json: async () => body,
  } as Response;
}

interface FetchCall {
  url: string;
  init?: RequestInit;
}

let fetchMock: ReturnType<typeof vi.fn>;
let fetchCalls: FetchCall[];

function installFetch(opts: { org?: typeof ADMIN_ORG; speakerList?: unknown[] } = {}) {
  fetchCalls = [];
  const org = opts.org ?? ADMIN_ORG;
  const speakerList = opts.speakerList ?? SPEAKER_FIXTURES;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    fetchCalls.push({ url, init });

    if (url.endsWith("/api/auth/me")) {
      return jsonResponse(meWith(org));
    }
    if (url.endsWith("/api/speakers")) {
      return jsonResponse(speakerList);
    }
    if (url.match(/\/api\/speakers\/\d+$/)) {
      return jsonResponse(SPEAKER_DETAIL);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
}

function installLocalStorage() {
  const store: Record<string, string> = {
    access_token: "test-token",
    "meetingops.activeOrg": "magic-unicorn",
  };
  vi.stubGlobal("localStorage", {
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

async function renderLibrary() {
  await act(async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <OrgProvider>
            <SpeakerLibrary />
          </OrgProvider>
        </AuthProvider>
      </MemoryRouter>
    );
  });
}

describe("SpeakerLibrary", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders enrolled speakers and forwards the org header", async () => {
    installFetch();
    await renderLibrary();

    await waitFor(() => expect(screen.getByText(/Speaker Library/i)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Aaron Stransky")).toBeInTheDocument());
    expect(screen.getByText("Shafen Khan")).toBeInTheDocument();

    const speakersCall = fetchCalls.find(c => c.url.endsWith("/api/speakers") && c.init?.method !== "POST");
    expect(speakersCall).toBeTruthy();
    const headers = new Headers(speakersCall?.init?.headers as HeadersInit);
    expect(headers.get("X-MeetingOps-Org")).toBe("magic-unicorn");
  });

  it("loads detail when a speaker is clicked", async () => {
    installFetch();
    await renderLibrary();

    await waitFor(() => expect(screen.getByText("Aaron Stransky")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Aaron Stransky"));

    await waitFor(() => expect(screen.getByText(/Enrollment samples/i)).toBeInTheDocument());
    expect(await screen.findByText(/4 meetings linked/i)).toBeInTheDocument();
    expect(screen.getByText(/8.4s/)).toBeInTheDocument();
  });

  it("blocks non-admins with a polite admin-only notice", async () => {
    installFetch({ org: USER_ORG, speakerList: [] });
    await renderLibrary();

    await waitFor(() => expect(screen.getByText(/restricted to organisation admins/i)).toBeInTheDocument());
    // Make sure we never even fetched the speaker list for non-admins
    expect(fetchCalls.find(c => c.url.endsWith("/api/speakers"))).toBeFalsy();
  });
});
