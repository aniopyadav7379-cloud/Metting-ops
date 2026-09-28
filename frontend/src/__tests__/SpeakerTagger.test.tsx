import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { SpeakerTagger } from "../components/SpeakerTagger";
import { AuthProvider } from "../contexts/AuthContext";
import { OrgProvider } from "../contexts/OrgContext";

const ORG = {
  id: 1,
  name: "Magic Unicorn",
  slug: "magic-unicorn",
  role: "user",
  is_active: true,
};

const ME = {
  id: 1,
  email: "tester@example.com",
  username: "tester",
  is_active: true,
  is_verified: true,
  is_superuser: false,
  created_at: "2026-05-03T00:00:00Z",
  organizations: [ORG],
  active_organization: ORG,
};

const SPEAKERS = [
  {
    id: 1,
    organization_id: 1,
    display_name: "Aaron Stransky",
    sample_count: 3,
    has_centroid: true,
    embedding_dim: 192,
    embedding_model: "speechbrain/spkrec-ecapa-voxceleb",
  },
  {
    id: 2,
    organization_id: 1,
    display_name: "Shafen Khan",
    sample_count: 1,
    has_centroid: true,
    embedding_dim: 192,
    embedding_model: "speechbrain/spkrec-ecapa-voxceleb",
  },
];

const LINKS_BEFORE = [
  {
    id: 10,
    session_id: 42,
    raw_label: "SPEAKER_00",
    speaker_id: 1,
    speaker_name: "Aaron Stransky",
    similarity: 0.81,
    source: "auto",
    confirmed: false,
  },
  {
    id: 11,
    session_id: 42,
    raw_label: "SPEAKER_01",
    speaker_id: null,
    speaker_name: null,
    similarity: null,
    source: "auto",
    confirmed: false,
  },
];

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

function installFetch(opts: { linksAfterUpdate?: unknown } = {}) {
  fetchCalls = [];
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    fetchCalls.push({ url, init });

    if (url.endsWith("/api/auth/me")) {
      return jsonResponse(ME);
    }
    if (url.includes("/api/speakers?session_id=42")) {
      return jsonResponse(SPEAKERS);
    }
    if (url.match(/\/speaker-links$/)) {
      return jsonResponse(LINKS_BEFORE);
    }
    if (url.match(/\/speaker-links\/11$/) && init?.method === "PATCH") {
      const body = JSON.parse(init.body as string);
      return jsonResponse({
        ...LINKS_BEFORE[1],
        speaker_id: body.speaker_id,
        speaker_name: body.speaker_id === 2 ? "Shafen Khan" : null,
        confirmed: !!body.confirmed,
        source: "manual",
      });
    }
    if (url.endsWith("/identify-speakers") && init?.method === "POST") {
      return jsonResponse({ session_id: 42, linked: 2, unmatched: 0, backend: "pyannote-3.1" });
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

async function renderTagger(
  props: React.ComponentProps<typeof SpeakerTagger> = { sessionId: "42" },
) {
  await act(async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <OrgProvider>
            <SpeakerTagger {...props} />
          </OrgProvider>
        </AuthProvider>
      </MemoryRouter>
    );
  });
}

describe("SpeakerTagger", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("surfaces the auto-match similarity badge when reviewing all voices", async () => {
    installFetch();
    await renderTagger();

    await waitFor(() => expect(screen.getByText("SPEAKER_01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /All voices \(2\)/i }));
    expect(await screen.findByText("SPEAKER_00")).toBeInTheDocument();
    expect(screen.getByText("SPEAKER_01")).toBeInTheDocument();
    // 0.81 -> 81%
    expect(screen.getByText("81%")).toBeInTheDocument();
    // SPEAKER_01 has no match
    expect(screen.getByText(/no match/i)).toBeInTheDocument();
    expect(
      fetchCalls.some((call) => call.url.includes("/api/speakers?session_id=42")),
    ).toBe(true);
  });

  it("starts with an unidentified-first review queue and can reveal all voices", async () => {
    installFetch();
    await renderTagger();

    await waitFor(() => expect(screen.getByText("SPEAKER_01")).toBeInTheDocument());
    expect(screen.getByText(/Needs a name \(1\)/i)).toBeInTheDocument();
    expect(screen.queryByText("SPEAKER_00")).not.toBeInTheDocument();
    expect(screen.getByText(/Unidentified voice/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /All voices \(2\)/i }));
    expect(await screen.findByText("SPEAKER_00")).toBeInTheDocument();
    expect(screen.getByText(/Suggested identity/i)).toBeInTheDocument();
  });

  it("filters the review queue by voice label or self-introduction suggestion", async () => {
    const linksWithSuggestion = [{
      ...LINKS_BEFORE[1],
      name_suggestions: [{ name: "Nora", evidence: "Hi, I'm Nora." }],
    }, LINKS_BEFORE[0]];
    installFetch();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      fetchCalls.push({ url, init });
      if (url.endsWith("/api/auth/me")) return jsonResponse(ME);
      if (url.includes("/api/speakers?session_id=42")) return jsonResponse(SPEAKERS);
      if (url.match(/\/speaker-links$/)) return jsonResponse(linksWithSuggestion);
      return jsonResponse({}, 404);
    });

    await renderTagger();
    await waitFor(() => expect(screen.getByText("SPEAKER_01")).toBeInTheDocument());
    fireEvent.change(screen.getByRole("searchbox", { name: /search speaker labels/i }), {
      target: { value: "Nora" },
    });
    expect(screen.getByText(/Showing 1 of 2 voices/i)).toBeInTheDocument();
    expect(screen.getByText("SPEAKER_01")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /clear speaker search/i }));
    expect(screen.getByText(/Showing 1 of 2 voices/i)).toBeInTheDocument();
  });

  it("PATCHes a manual confirmation when a user picks a speaker for an unmatched label", async () => {
    installFetch();
    await renderTagger();

    await waitFor(() => expect(screen.getByText("SPEAKER_01")).toBeInTheDocument());
    const select = screen.getByLabelText(/Speaker for SPEAKER_01/i) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "2" } });

    await waitFor(() => {
      const patchCall = fetchCalls.find(
        c => c.url.endsWith("/speaker-links/11") && c.init?.method === "PATCH"
      );
      expect(patchCall).toBeTruthy();
      const body = JSON.parse(patchCall?.init?.body as string);
      expect(body.speaker_id).toBe(2);
      expect(body.confirmed).toBe(true);
      const headers = new Headers(patchCall?.init?.headers as HeadersInit);
      expect(headers.get("X-MeetingOps-Org")).toBe("magic-unicorn");
    });
  });

  it("plays representative moments from the existing meeting audio", async () => {
    installFetch();
    const onPlaySample = vi.fn();
    await renderTagger({
      sessionId: "42",
      onPlaySample,
      segments: [
        {
          start: 12,
          end: 20,
          text: "This is a clear representative sentence from Shafen.",
          speaker: "SPEAKER_01",
          raw_label: "SPEAKER_01",
        },
      ],
    });

    const sample = await screen.findByTitle(/Play SPEAKER_01 at 0:12/i);
    fireEvent.click(sample);
    expect(onPlaySample).toHaveBeenCalledWith(12, 20);
    expect(
      screen.getByText(/not saved as separate biometric clips/i),
    ).toBeInTheDocument();
  });
});
