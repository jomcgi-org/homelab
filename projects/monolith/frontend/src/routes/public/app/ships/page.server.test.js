import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";
import { SHIPS_SNAPSHOT_CACHE_CONTROL } from "../../../../lib/cache-headers.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const SNAPSHOT = { count: 0, vessels: [] };

describe("/public/app/ships load", () => {
  it("hits the SSR-only ships snapshot endpoint", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => SNAPSHOT,
    });

    await load({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledTimes(1);
    const url = fetch.mock.calls[0][0];
    expect(url).toMatch(/\/api\/ships\/snapshot$/);
  });

  it("sets the shared snapshot cache-control header", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => SNAPSHOT,
    });

    const result = await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": SHIPS_SNAPSHOT_CACHE_CONTROL,
      }),
    );
    // Byte-for-byte mirror of _SNAPSHOT_CACHE_CONTROL in ships/router.py.
    expect(SHIPS_SNAPSHOT_CACHE_CONTROL).toBe(
      "public, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400",
    );
    expect(result.snapshot).toEqual(SNAPSHOT);
  });

  it("forwards ETag and Last-Modified from the API response", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"2026-06-10T00:00:00+00:00-3"',
        "Last-Modified": "Wed, 10 Jun 2026 00:00:00 GMT",
      }),
      json: async () => SNAPSHOT,
    });

    await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        etag: '"2026-06-10T00:00:00+00:00-3"',
        "last-modified": "Wed, 10 Jun 2026 00:00:00 GMT",
      }),
    );
  });

  it("omits ETag and Last-Modified when the API does not return them", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => SNAPSHOT,
    });

    await load({ fetch, setHeaders });

    const headers = setHeaders.mock.calls[0][0];
    expect(headers).not.toHaveProperty("etag");
    expect(headers).not.toHaveProperty("last-modified");
  });

  it("throws a 503 when the backend fetch fails", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 });

    await expect(load({ fetch, setHeaders })).rejects.toThrow();
  });
});
