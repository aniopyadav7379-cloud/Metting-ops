import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { SpeakerLibrary } from "../pages/SpeakerLibrary";
import { AuthProvider } from "../contexts/AuthContext";
import { OrgProvider } from "../contexts/OrgContext";

// Mirrors SpeakerLibrary.unassigned.test.tsx harness. Focus: the
// text-derived self-introduction suggestion chip on an UNIDENTIFIED
// speaker (name_suggestions). The chip must be suggestion-only — clicking
// it prefills the existing Create-new-speaker input inside the Assign
// dialog and never fires an assign/create on its own.

const ADMIN_ORG = {
  id: 1,
  name: "Magic Unicorn",
  slug: "magic-unicorn",
  role: "admin",
  is_active: true,
};

const ME_PAYLOAD = {
  id: 1,
  email: "test@example.com",
  username: "tester",
  is_active: true,
  is_verified: true,
  is_superuser: false,
  created_at: "2026-05-03T00:00:00Z",
  organizations: [ADMIN_ORG],
  active_organization: ADMIN_ORG,
};

const SPEAKERS = [
  {
    id: 7,
    organization_id: 1,
    display_name: "Aaron Stransky",
    email: "aaron@magicunicorn.tech",
    notes: null,
    embedding_dim: 192,
    embedding_model: "test-model",
    sample_count: 2,
    has_centroid: true,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-02T00:00:00Z",
  },
];

// One unassigned link with NO voiceprint match but a self-intro name
// suggestion derived from the speaker's own words.
const UNASSIGNED = {
  items: [
    {
      id: 44,
      raw_label: "SPEAKER_00",
      session_id: "session-abc",
      session_pk: 12,
      session_title: "Planning Meeting",
      session_started_at: "2026-05-01T12:00:00Z",
      meeting_date: null,
      duration_seconds: 64,
      segment_count: 3,
      preview: "Hi, I'm John, let's get started.",
      sample_audio_url: null,
      top_matches: [],
      name_suggestions: [{ name: "John", evidence: "Hi, I'm John." }],
    },
  ],
  count: 1,
  total: 1,
  limit: 50,
  offset: 0,
  next_offset: null,
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

let fetchCalls: FetchCall[];

function installFetch() {
  fetchCalls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    fetchCalls.push({ url, init });

    if (url.endsWith("/api/auth/me")) return jsonResponse(ME_PAYLOAD);
    if (url.includes("/api/speakers/unassigned-links")) return jsonResponse(UNASSIGNED);
    if (url.endsWith("/api/speakers") && init?.method !== "POST") return jsonResponse(SPEAKERS);
    return jsonResponse({}, 404);
  }));
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

describe("SpeakerLibrary self-introduction suggestions", () => {
  beforeEach(() => {
    installLocalStorage();
    installFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a 'Suggested: <name>' chip on an unidentified speaker", async () => {
    await renderLibrary();

    expect(await screen.findByText("Planning Meeting")).toBeInTheDocument();
    expect(await screen.findByText(/Heard them introduce themselves/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Suggested: John/i })).toBeInTheDocument();
  });

  it("prefills the create-speaker name on click WITHOUT firing an assign/create", async () => {
    await renderLibrary();

    fireEvent.click(await screen.findByRole("button", { name: /Suggested: John/i }));

    // The Assign dialog opens with the create-new-speaker input prefilled.
    const input = (await screen.findByPlaceholderText("New speaker name")) as HTMLInputElement;
    expect(input.value).toBe("John");

    // Critical: clicking the suggestion is NOT a mutation. Only the GET
    // reads happened — no POST /api/speakers and no PATCH speaker-links.
    const mutated = fetchCalls.find(
      c => c.init?.method === "POST" || c.init?.method === "PATCH",
    );
    expect(mutated).toBeUndefined();
  });
});
