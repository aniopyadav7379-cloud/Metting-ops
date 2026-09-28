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
  {
    id: 8,
    organization_id: 1,
    display_name: "Shafen Khan",
    email: null,
    notes: null,
    embedding_dim: 192,
    embedding_model: "test-model",
    sample_count: 1,
    has_centroid: true,
    created_at: "2026-05-02T00:00:00Z",
    updated_at: "2026-05-02T00:00:00Z",
  },
];

const DETAIL_AARON = {
  ...SPEAKERS[0],
  voice_samples: [],
  session_count: 4,
};

const DETAIL_SHAFEN = {
  ...SPEAKERS[1],
  sample_count: 3,
  voice_samples: [],
  session_count: 5,
};

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
      preview: "We discussed launch work.",
      sample_audio_url: null,
      top_matches: [
        { speaker_id: 7, display_name: "Aaron Stransky", similarity: 0.91 },
        { speaker_id: 8, display_name: "Shafen Khan", similarity: 0.62 },
      ],
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
    if (url.endsWith("/api/speakers/7")) return jsonResponse(DETAIL_AARON);
    if (url.endsWith("/api/speakers/8")) return jsonResponse(DETAIL_SHAFEN);
    if (url.endsWith("/api/speakers/8/merge") && init?.method === "POST") return jsonResponse(DETAIL_SHAFEN);
    if (url.includes("/api/sessions/session-abc/speaker-links/44") && init?.method === "PATCH") {
      return jsonResponse({
        id: 44,
        session_id: 12,
        raw_label: "SPEAKER_00",
        speaker_id: 7,
        speaker_name: "Aaron Stransky",
        similarity: null,
        source: "manual",
        confirmed: true,
      });
    }
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

describe("SpeakerLibrary unassigned fingerprints and merge", () => {
  beforeEach(() => {
    installLocalStorage();
    installFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders unassigned fingerprints and assigns via suggested match chip", async () => {
    await renderLibrary();

    expect(await screen.findByText(/Unassigned voice fingerprints/i)).toBeInTheDocument();
    expect(await screen.findByText("Planning Meeting")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Aaron Stransky 0.91/i }));

    await waitFor(() => {
      const patch = fetchCalls.find(call =>
        call.url.includes("/api/sessions/session-abc/speaker-links/44")
        && call.init?.method === "PATCH"
      );
      expect(patch).toBeTruthy();
      expect(JSON.parse(patch?.init?.body as string)).toEqual({
        speaker_id: 7,
        confirmed: true,
      });
    });
  });

  it("merge modal requires typed confirmation and refreshes after success", async () => {
    await renderLibrary();

    expect(await screen.findByText("Aaron Stransky")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Aaron Stransky"));
    expect(await screen.findByText(/4 meetings linked/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Merge into another speaker/i }));
    const destructive = screen.getByRole("button", { name: /Merge and delete source/i });
    expect(destructive).toBeDisabled();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "8" } });
    expect(destructive).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Type/i), { target: { value: "aaron stransky" } });
    expect(destructive).not.toBeDisabled();
    fireEvent.click(destructive);

    await waitFor(() => {
      const merge = fetchCalls.find(call =>
        call.url.endsWith("/api/speakers/8/merge") && call.init?.method === "POST"
      );
      expect(merge).toBeTruthy();
      expect(JSON.parse(merge?.init?.body as string)).toEqual({ source_id: 7 });
    });
  });
});
