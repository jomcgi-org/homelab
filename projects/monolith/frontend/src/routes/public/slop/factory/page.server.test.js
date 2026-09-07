import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const payloads = {
  "/slop/factory/activity": { now: {}, daily: [], totals_7d: {} },
  "/slop/factory/merges": { daily: [], week: [], totals: {} },
  "/slop/factory/entities": [{ kind: "project", slug: "embervm" }],
};

function response(path, ok = true) {
  return {
    ok,
    headers: { get: () => null },
    json: async () =>
      path.startsWith("/slop/factory/entities/project/")
        ? { notes: [{ note_id: "fact" }] }
        : payloads[path],
  };
}

describe("factory overview loader", () => {
  it("loads live inputs through same-origin proxies", async () => {
    const fetch = vi.fn((path) => Promise.resolve(response(path)));
    const result = await load({ fetch, setHeaders: vi.fn() });

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/slop/factory/activity",
      "/slop/factory/merges",
      "/slop/factory/entities",
      "/slop/factory/entities/project/embervm/notes?state=verified%2Cunverified&limit=60",
    ]);
    expect(result.activity).toEqual(payloads["/slop/factory/activity"]);
    expect(result.merges).toEqual(payloads["/slop/factory/merges"]);
    expect(result.entities).toEqual(payloads["/slop/factory/entities"]);
    expect(result.facts).toEqual([{ note_id: "fact" }]);
  });

  it("fails closed when a required feed is unavailable", async () => {
    const fetch = vi.fn((path) =>
      Promise.resolve(response(path, path !== "/slop/factory/merges")),
    );

    await expect(load({ fetch, setHeaders: vi.fn() })).rejects.toMatchObject({
      status: 503,
    });
  });
});
