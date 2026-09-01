import { beforeEach, describe, expect, it, vi } from "vitest";

// The route reads API_BASE at module scope, so stub it before the import
// (imports are hoisted; only vi.hoisted runs earlier).
vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { load } from "./+page.server.js";

function jsonResponse(body, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body };
}

describe("agents /private/agents load", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("passes sessions and the model catalogue through untouched", async () => {
    const sessions = [{ id: 1 }];
    const catalog = {
      models: [{ name: "luna", family: "codex" }],
    };
    const fetchMock = vi.fn(async (url) =>
      jsonResponse(String(url).includes("models") ? catalog : sessions),
    );

    const result = await load({ fetch: fetchMock });

    expect(result).toEqual({
      sessions,
      models: catalog.models,
      error: false,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://backend/api/agents/models",
      expect.anything(),
    );
  });

  it("renders an EMPTY catalogue as empty: no bundled fallback list", async () => {
    const fetchMock = vi.fn(async (url) =>
      jsonResponse(String(url).includes("models") ? { models: [] } : []),
    );

    const result = await load({ fetch: fetchMock });

    expect(result.sessions).toEqual([]);
    expect(result.models).toEqual([]);
    expect(result.error).toBe(false);
  });

  it("marks error and empties both lists when either endpoint fails", async () => {
    const fetchMock = vi.fn(async (url) =>
      jsonResponse([], !String(url).includes("models")),
    );

    const result = await load({ fetch: fetchMock });

    expect(result).toEqual({ sessions: [], models: [], error: true });
  });
});
