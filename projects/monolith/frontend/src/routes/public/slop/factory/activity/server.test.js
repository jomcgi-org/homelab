import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([key, value]) => [key.toLowerCase(), value]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const activity = {
  now: {
    active_last_hour: 1,
    sessions_today: 2,
    running: 1,
    last_turn_at: null,
  },
  daily: [],
  totals_7d: {
    sessions: 0,
    turns: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cost_usd: null,
    list_cost_usd: null,
  },
};

describe("/public/slop/factory/activity GET", () => {
  it("proxies the public agent activity endpoint unchanged", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => activity,
    });

    const response = await GET({ fetch, setHeaders });

    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/agents\/public\/activity$/);
    expect(await response.json()).toEqual(activity);
  });

  it("sets five-minute browser and Cloudflare cache headers", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({ ETag: '"activity"' }),
      json: async () => activity,
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "public, max-age=300, s-maxage=300",
      "cloudflare-cdn-cache-control": "public, max-age=300",
      etag: '"testbuild-activity"',
    });
  });

  it("returns 503 when the backend request fails", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false });

    await expect(GET({ fetch, setHeaders: vi.fn() })).rejects.toThrow();
  });
});
