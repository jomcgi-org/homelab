import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { load } from "./+page.server.js";

function response(body, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body };
}

describe("private updates load", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("passes shareable facet filters to the archive API", async () => {
    const archive = {
      updates: [{ published_on: "2026-08-29" }],
      projects: [{ value: "monolith", count: 1 }],
      technologies: [{ value: "frontend", count: 1 }],
    };
    const fetchMock = vi.fn(async () => response(archive));
    const url = new URL(
      "https://private.jomcgi.dev/updates?project=monolith&technology=frontend",
    );

    const result = await load({ fetch: fetchMock, url });

    const endpoint = fetchMock.mock.calls[0][0];
    expect(endpoint.toString()).toBe(
      "http://backend/api/updates?project=monolith&technology=frontend",
    );
    expect(result).toEqual({
      ...archive,
      selectedProject: "monolith",
      selectedTechnology: "frontend",
      error: false,
    });
  });

  it("returns a renderable empty state when the API fails", async () => {
    const result = await load({
      fetch: vi.fn(async () => response({}, false)),
      url: new URL("https://private.jomcgi.dev/updates?project=monolith"),
    });

    expect(result).toEqual({
      updates: [],
      projects: [],
      technologies: [],
      selectedProject: "monolith",
      selectedTechnology: "",
      error: true,
    });
  });
});
