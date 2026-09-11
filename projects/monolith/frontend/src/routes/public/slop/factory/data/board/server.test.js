import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([key, value]) => [key.toLowerCase(), value]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const board = {
  snapshotted_at: "2026-09-11T14:32:00Z",
  state: "running",
  policy: { generation: 7, max_tasks: 2 },
  active: [],
  queued: [],
  recent: [],
};

describe("/public/slop/factory/data/board GET", () => {
  it("proxies the public factory activity endpoint unchanged", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => board,
    });

    const response = await GET({ fetch, setHeaders: vi.fn() });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/agents\/public\/factory\/activity$/,
    );
    expect(await response.json()).toEqual(board);
  });

  it("sets one-minute browser and Cloudflare cache headers", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({ ETag: '"board"' }),
      json: async () => board,
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "public, max-age=60, s-maxage=60",
      "cloudflare-cdn-cache-control": "public, max-age=60",
      etag: '"testbuild-board"',
    });
  });

  it("returns 503 when the backend request fails", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 });

    await expect(GET({ fetch, setHeaders: vi.fn() })).rejects.toMatchObject({
      status: 503,
    });
  });
});
