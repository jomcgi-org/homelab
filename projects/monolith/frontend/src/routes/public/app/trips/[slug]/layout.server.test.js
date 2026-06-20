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
    expect(TRIPS_CACHE_CONTROL).toBe(
      "public, max-age=60, s-maxage=300, stale-while-revalidate=3600",
    );
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

  it("pre-signs image URLs onto points, the cover, and highlights", async () => {
    // Clear the signing secret so the signer's deterministic unsigned /unsafe/
    // fallback is exercised (the HMAC path is covered in lib/server/trips-img.test.js).
    const savedKey = process.env.IMGPROXY_KEY;
    const savedSalt = process.env.IMGPROXY_SALT;
    delete process.env.IMGPROXY_KEY;
    delete process.env.IMGPROXY_SALT;
    try {
      const payload = {
        trip: {
          slug: "2025-x",
          title: "X",
          default_image: "cover.jpg",
          highlights: [
            { id: 1, title: "H1", image: "h1.jpg" },
            { id: 2, title: "no-photo" },
          ],
        },
        points: [
          { id: 1, image: "p1.jpg" },
          { id: 2, lat: 1, lng: 2 },
        ],
      };
      const fetch = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: makeHeaders(),
        json: async () => payload,
      });

      const result = await load({
        params: { slug: "2025-x" },
        fetch,
        setHeaders: vi.fn(),
      });

      expect(result.trip.coverUrl).toBe(
        "/img/unsafe/gallery/plain/s3://monolith-trips/cover.jpg",
      );
      expect(result.trip.highlights[0].imgGallery).toBe(
        "/img/unsafe/gallery/plain/s3://monolith-trips/h1.jpg",
      );
      expect(result.trip.highlights[1].imgGallery).toBeUndefined();
      expect(result.points[0].imgDisplay).toBe(
        "/img/unsafe/display/plain/s3://monolith-trips/p1.jpg",
      );
      expect(result.points[0].imgGallery).toBe(
        "/img/unsafe/gallery/plain/s3://monolith-trips/p1.jpg",
      );
      // Image-less point passes through untouched.
      expect(result.points[1].imgDisplay).toBeUndefined();
    } finally {
      if (savedKey === undefined) delete process.env.IMGPROXY_KEY;
      else process.env.IMGPROXY_KEY = savedKey;
      if (savedSalt === undefined) delete process.env.IMGPROXY_SALT;
      else process.env.IMGPROXY_SALT = savedSalt;
    }
  });
});
