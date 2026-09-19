import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { load } from "./+page.server.js";

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

describe("private updates load", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("loads the newest available month without requesting the full archive", async () => {
    const archive = {
      updates: [{ published_on: "2026-08-29" }],
      months: [
        {
          month: "2026-08",
          count: 1,
          editions: [{ published_on: "2026-08-29", headline: "Newest" }],
        },
      ],
      projects: [{ value: "monolith", count: 1 }],
      technologies: [{ value: "frontend", count: 1 }],
      selected_month: "2026-08",
    };
    const fetchMock = vi.fn(async () => response(archive));
    const url = new URL("https://private.jomcgi.dev/updates");

    const result = await load({ fetch: fetchMock, url });

    const endpoint = fetchMock.mock.calls[0][0];
    expect(endpoint.toString()).toBe("http://backend/api/updates");
    expect(result).toEqual({
      updates: archive.updates,
      months: archive.months,
      projects: archive.projects,
      technologies: archive.technologies,
      selectedMonth: "2026-08",
      selectedProject: "",
      selectedTechnology: "",
      invalidMonth: false,
      error: false,
    });
  });

  it("passes an older month and both facet filters to the archive API", async () => {
    const archive = {
      updates: [],
      months: [
        {
          month: "2026-08",
          count: 1,
          editions: [{ published_on: "2026-08-29", headline: "Newest" }],
        },
      ],
      projects: [{ value: "monolith", count: 1 }],
      technologies: [{ value: "frontend", count: 1 }],
      selected_month: "2025-12",
    };
    const fetchMock = vi.fn(async () => response(archive));
    const url = new URL(
      "https://private.jomcgi.dev/updates?month=2025-12&project=monolith&technology=frontend",
    );

    const result = await load({ fetch: fetchMock, url });

    expect(fetchMock.mock.calls[0][0].toString()).toBe(
      "http://backend/api/updates?project=monolith&technology=frontend&month=2025-12",
    );
    expect(result.selectedMonth).toBe("2025-12");
    expect(result.months).toEqual(archive.months);
    expect(result.updates).toEqual([]);
  });

  it("returns the archive-ready state when no editions exist", async () => {
    const result = await load({
      fetch: vi.fn(async () =>
        response({
          updates: [],
          months: [],
          projects: [],
          technologies: [],
          selected_month: null,
        }),
      ),
      url: new URL("https://private.jomcgi.dev/updates"),
    });

    expect(result.error).toBe(false);
    expect(result.selectedMonth).toBe("");
    expect(result.months).toEqual([]);
  });

  it("returns a distinct state for an invalid direct month", async () => {
    const result = await load({
      fetch: vi.fn(async () => response({}, 422)),
      url: new URL(
        "https://private.jomcgi.dev/updates?project=monolith&month=2026-13",
      ),
    });

    expect(result).toEqual({
      updates: [],
      months: [],
      projects: [],
      technologies: [],
      selectedMonth: "2026-13",
      selectedProject: "monolith",
      selectedTechnology: "",
      invalidMonth: true,
      error: false,
    });
  });

  it("keeps upstream failures distinct from an invalid direct month", async () => {
    const result = await load({
      fetch: vi.fn(async () => response({}, 503)),
      url: new URL("https://private.jomcgi.dev/updates?month=2026-01"),
    });

    expect(result.invalidMonth).toBe(false);
    expect(result.error).toBe(true);
  });
});
