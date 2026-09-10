import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { load } from "./+page.server.js";

function jsonResponse(body, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body };
}

describe("agents /private/agents/factory load", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("passes the board through and forwards the selected task", async () => {
    const board = {
      ok: true,
      state: "enabled",
      active: [],
      queued: [],
      recent: [],
    };
    const fetchMock = vi.fn(async () => jsonResponse(board));

    const result = await load({
      fetch: fetchMock,
      url: new URL("https://private.jomcgi.dev/agents/factory?task=t-1"),
      untrack: (fn) => fn(),
    });

    expect(result).toEqual({ board, task: "t-1", error: false });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://backend/api/agents/factory?task=t-1",
      expect.anything(),
    );
  });

  it("reports an unavailable backend without throwing", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}, false));

    const result = await load({
      fetch: fetchMock,
      url: new URL("https://private.jomcgi.dev/agents/factory"),
      untrack: (fn) => fn(),
    });

    expect(result).toEqual({ board: null, task: null, error: true });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://backend/api/agents/factory",
      expect.anything(),
    );
  });
});
