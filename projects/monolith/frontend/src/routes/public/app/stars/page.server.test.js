import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";
import { STARS_SITES_CACHE_CONTROL } from "../../../../lib/cache-headers.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const SNAPSHOT = { sites: [], count: 0, total_sites: 30, fetched_at: null };

describe("/public/app/stars load", () => {
  it("hits the SSR-only stars sites endpoint", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => SNAPSHOT,
    });

    await load({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledTimes(1);
    const url = fetch.mock.calls[0][0];
    expect(url).toMatch(/\/api\/stars\/sites$/);
  });

  it("sets the shared sites cache-control header", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => SNAPSHOT,
    });

    const result = await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": STARS_SITES_CACHE_CONTROL,
      }),
    );
    // Byte-for-byte mirror of _SITES_CACHE_CONTROL in stars/router.py.
    expect(STARS_SITES_CACHE_CONTROL).toBe(
      "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400",
    );
    expect(result.snapshot).toEqual(SNAPSHOT);
    expect(result.renderedAt).toEqual(expect.any(Number));
  });

  it("shares identical forecast hours while retaining every upstream field", async () => {
    const hour = {
      time: "2026-11-14T22:00:00+00:00",
      cloud_area_fraction: 4.5,
      dark: true,
      future_metadata: { confidence: [0.5, 0.9] },
    };
    const snapshot = {
      ...SNAPSHOT,
      future_metadata: "retained",
      sites: [
        {
          id: "first",
          future_field: null,
          best_hours: [structuredClone(hour)],
        },
        { id: "second", best_hours: [structuredClone(hour)] },
      ],
    };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => snapshot,
    });
    const { snapshot: result } = await load({ fetch, setHeaders: vi.fn() });
    expect(JSON.stringify(result)).toBe(JSON.stringify(snapshot));
    expect(result.sites[0].best_hours[0]).toBe(result.sites[1].best_hours[0]);
    expect(snapshot.sites[0].best_hours[0]).not.toBe(
      snapshot.sites[1].best_hours[0],
    );
  });

  it("leaves HTML validation to SvelteKit when the upstream data is unchanged", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"v1-2026-06-13T21:00:00+00:00-none-0"',
        "Last-Modified": "Fri, 13 Jun 2026 21:00:00 GMT",
      }),
      json: async () => SNAPSHOT,
    });

    await load({ fetch, setHeaders });

    const headers = setHeaders.mock.calls[0][0];
    expect(headers).not.toHaveProperty("etag");
    expect(headers).not.toHaveProperty("last-modified");
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

  it("retries transient backend failures once another pod can answer", async () => {
    const setHeaders = vi.fn();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: makeHeaders(),
        json: async () => SNAPSHOT,
      });

    await load({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("serializes a renewed clock without reusing a data-only validator", async () => {
    const clock = vi.spyOn(Date, "now");
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"same-data"',
        "Last-Modified": "Mon, 19 Oct 2026 12:00:00 GMT",
      }),
      json: async () => SNAPSHOT,
    });
    try {
      clock.mockReturnValue(Date.parse("2026-10-19T23:59:00Z"));
      const before = await load({ fetch, setHeaders });
      clock.mockReturnValue(Date.parse("2026-10-20T00:01:00Z"));
      const after = await load({ fetch, setHeaders });
      expect(after.snapshot).toEqual(before.snapshot);
      expect(after.renderedAt).toBeGreaterThan(before.renderedAt);
      for (const [headers] of setHeaders.mock.calls) {
        expect(headers).not.toHaveProperty("etag");
        expect(headers).not.toHaveProperty("last-modified");
      }
    } finally {
      clock.mockRestore();
    }
  });
});
