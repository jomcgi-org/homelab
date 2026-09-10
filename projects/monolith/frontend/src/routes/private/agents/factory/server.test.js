import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { GET } from "./+server.js";

describe("agents factory proxy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = undefined;
  });

  it("relays the board and forwards the task query", async () => {
    const board = { ok: true, state: "enabled" };
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => board,
    }));

    const res = await GET({
      url: new URL("https://private.jomcgi.dev/agents/factory?task=t-1"),
    });

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(board);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "http://backend/api/agents/factory?task=t-1",
      expect.anything(),
    );
  });

  it("answers 502 when the backend is down", async () => {
    globalThis.fetch = vi.fn(async () => ({ ok: false, status: 503 }));

    const res = await GET({
      url: new URL("https://private.jomcgi.dev/agents/factory"),
    });

    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "factory unavailable" });
  });
});
