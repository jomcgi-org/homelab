import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

function event(fetch, { scope, email = "joe@example.test" } = {}) {
  const url = new URL("https://friends.jomcgi.dev/moving");
  if (scope) url.searchParams.set("scope", scope);
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
    expect(result).toEqual({ status: "ready", scope: "all", state });
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

  it("turns a backend 403 into the recognised page state", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 403 });

    await expect(load(event(fetch))).resolves.toEqual({
      status: "forbidden",
      scope: "mine",
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
