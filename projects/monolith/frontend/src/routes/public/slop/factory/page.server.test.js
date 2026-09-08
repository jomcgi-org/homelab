import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const payloads = {
  "/slop/factory/activity": {
    now: {},
    daily: [],
    local_daily: [],
    totals_7d: { ember: {}, local: {} },
  },
  "/slop/factory/merges": { daily: [], week: [], totals: {} },
  "/slop/factory/facts": {
    daily: [],
    totals: { verified: 1, unverified: 2, disputed: 0 },
    contradictions: 0,
  },
};

function response(path, ok = true) {
  return {
    ok,
    headers: { get: () => null },
    json: async () => payloads[path],
  };
}

describe("factory overview loader", () => {
  it("loads live inputs through same-origin proxies", async () => {
    const fetch = vi.fn((path) => Promise.resolve(response(path)));
    const result = await load({ fetch, setHeaders: vi.fn() });

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/slop/factory/activity",
      "/slop/factory/merges",
      "/slop/factory/facts",
    ]);
    expect(result.activity).toEqual(payloads["/slop/factory/activity"]);
    expect(result.merges).toEqual(payloads["/slop/factory/merges"]);
    expect(result.facts).toEqual(payloads["/slop/factory/facts"]);
  });

  it.each([
    ["/slop/factory/activity", "activity"],
    ["/slop/factory/merges", "merges"],
    ["/slop/factory/facts", "facts"],
  ])("keeps rendering when %s returns 503", async (failedPath, section) => {
    const fetch = vi.fn((path) =>
      Promise.resolve(response(path, path !== failedPath)),
    );

    const result = await load({ fetch, setHeaders: vi.fn() });

    expect(result.unavailable[section]).toBe(true);
    expect(result.title).toBe("Factory");
  });
});
