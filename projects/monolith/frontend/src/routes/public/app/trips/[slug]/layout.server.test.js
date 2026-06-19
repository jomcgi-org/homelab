import { describe, it, expect, vi } from "vitest";
import { load } from "./+layout.server.js";
import { TRIPS_CACHE_CONTROL } from "../../../../../lib/cache-headers.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const TRIP = { trip: { slug: "2025-x", title: "X" }, points: [] };

describe("/public/app/trips/[slug] layout load", () => {
  it("hits the SSR-only trip endpoint with the slug", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: makeHeaders(),
      json: async () => TRIP,
    });

    const result = await load({
      params: { slug: "2025-x" },
      fetch,
      setHeaders,
    });

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/trips\/trip\/2025-x$/);
    expect(result).toEqual(TRIP);
  });

  it("sets the shared trips cache-control header", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: makeHeaders(),
      json: async () => TRIP,
    });

    await load({ params: { slug: "2025-x" }, fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({ "cache-control": TRIPS_CACHE_CONTROL }),
    );
    // Byte-for-byte mirror of _CACHE in trips/read_router.py.
    expect(TRIPS_CACHE_CONTROL).toBe("public, max-age=300, s-maxage=86400");
  });

  it("throws a 404 when the trip does not exist", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    await expect(
      load({ params: { slug: "nope" }, fetch, setHeaders }),
    ).rejects.toThrow();
  });

  it("throws a 503 when the backend errors", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 });
    await expect(
      load({ params: { slug: "2025-x" }, fetch, setHeaders }),
    ).rejects.toThrow();
  });
});
