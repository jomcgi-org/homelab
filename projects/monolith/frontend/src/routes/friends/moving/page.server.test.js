import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

function event(fetch, { scope, mode, email = "joe@example.test" } = {}) {
  const url = new URL("https://friends.jomcgi.dev/moving");
  if (scope) url.searchParams.set("scope", scope);
  if (mode) url.searchParams.set("mode", mode);
  return {
    fetch,
    request: new Request(url, {
      headers: email ? { "X-Auth-Email": email } : {},
    }),
    url,
  };
}

describe("friends moving server load", () => {
  beforeEach(() => {
    process.env.API_BASE = "http://moving-api.test";
  });

  afterEach(() => {
    delete process.env.API_BASE;
    vi.restoreAllMocks();
  });

  it("loads the selected scope and forwards the viewer email", async () => {
    const state = { tasks: [], spans: [], progress: 0, viewer: "joe" };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => state,
    });

    const result = await load(event(fetch, { scope: "all" }));

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe(
      "http://moving-api.test/api/moving/state?scope=all",
    );
    expect(fetch.mock.calls[0][1].headers).toEqual({
      "X-Auth-Email": "joe@example.test",
    });
    expect(fetch.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
    expect(result).toEqual({ status: "ready", scope: "all", mode: "", state });
  });

  it("defaults the scope to mine", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ tasks: [] }),
    });

    await load(event(fetch));

    expect(fetch.mock.calls[0][0]).toBe(
      "http://moving-api.test/api/moving/state?scope=mine",
    );
  });

  it("passes through manage mode and forces the all scope", async () => {
    const state = { tasks: [] };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => state,
    });

    const result = await load(event(fetch, { scope: "mine", mode: "manage" }));

    expect(fetch.mock.calls[0][0]).toBe(
      "http://moving-api.test/api/moving/state?scope=all",
    );
    expect(result).toEqual({
      status: "ready",
      scope: "all",
      mode: "manage",
      state,
    });
  });

  it("passes through unknown modes without changing dashboard scope", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ tasks: [] }),
    });

    const result = await load(event(fetch, { scope: "mine", mode: "preview" }));

    expect(fetch.mock.calls[0][0]).toBe(
      "http://moving-api.test/api/moving/state?scope=mine",
    );
    expect(result.mode).toBe("preview");
  });

  it("turns a backend 403 into the recognised page state", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 403 });

    await expect(load(event(fetch))).resolves.toEqual({
      status: "forbidden",
      scope: "mine",
      mode: "",
      state: null,
    });
  });

  it("fails loudly when API_BASE is missing", async () => {
    delete process.env.API_BASE;
    const fetch = vi.fn();

    await expect(load(event(fetch))).rejects.toThrow(
      "API_BASE is required for the moving planner",
    );
    expect(fetch).not.toHaveBeenCalled();
  });
});
