import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";
import { DR_JOBS_LISTINGS_CACHE_CONTROL } from "../../../../lib/cache-headers.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const LISTINGS = { jobs: [] };

describe("/public/app/dr-jobs load", () => {
  it("hits the SSR-only dr-jobs listings endpoint", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => LISTINGS,
    });

    await load({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledTimes(1);
    const url = fetch.mock.calls[0][0];
    expect(url).toMatch(/\/api\/dr-jobs\/listings$/);
  });

  it("sets the shared listings cache-control header", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => LISTINGS,
    });

    const result = await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": DR_JOBS_LISTINGS_CACHE_CONTROL,
      }),
    );
    // Byte-for-byte mirror of _LISTINGS_CACHE_CONTROL in dr_jobs/router.py.
    expect(DR_JOBS_LISTINGS_CACHE_CONTROL).toBe(
      "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400",
    );
    expect(result.listings).toEqual(LISTINGS);
  });

  it("versions the API ETag with the build version so a layout deploy busts it", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"v1-2026-06-17-2026-06-17T05:00:00+00:00-6"',
      }),
      json: async () => LISTINGS,
    });

    await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        // Build version (testbuild, from the $app/environment stub) is spliced
        // inside the quotes. Without this, a layout-only deploy leaves the
        // data-derived ETag unchanged and browsers + the CDN keep 304-ing onto
        // pre-deploy HTML (the bug this route hit).
        etag: '"testbuild-v1-2026-06-17-2026-06-17T05:00:00+00:00-6"',
      }),
    );
  });

  it("omits ETag when the API does not return one", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => LISTINGS,
    });

    await load({ fetch, setHeaders });

    const headers = setHeaders.mock.calls[0][0];
    expect(headers).not.toHaveProperty("etag");
  });

  it("throws a 503 when the backend fetch fails", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 });

    await expect(load({ fetch, setHeaders })).rejects.toThrow();
  });
});
