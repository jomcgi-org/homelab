import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";
import { TRIPS_CACHE_CONTROL } from "../../../../lib/cache-headers.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const INDEX = { count: 0, trips: [] };

describe("/public/app/trips index load", () => {
  it("hits the SSR-only trips index endpoint", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => INDEX,
    });

    const result = await load({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/trips\/trips$/);
    expect(result.index).toEqual(INDEX);
    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({ "cache-control": TRIPS_CACHE_CONTROL }),
    );
  });

  it("throws when the backend fetch fails", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });
    await expect(load({ fetch, setHeaders })).rejects.toThrow();
  });
});
