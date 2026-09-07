import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([key, value]) => [key.toLowerCase(), value]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

const merges = {
  daily: [],
  week: [],
  totals: {},
};

describe("/public/slop/factory/merges GET", () => {
  it("proxies the public merges endpoint unchanged", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => merges,
    });

    const response = await GET({ fetch, setHeaders });

    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/agents\/public\/merges$/);
    expect(await response.json()).toEqual(merges);
  });

  it("sets cache headers and passes through versioned validators", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"merges"',
        "Last-Modified": "Mon, 07 Sep 2026 12:00:00 GMT",
      }),
      json: async () => merges,
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control":
        "public, max-age=0, s-maxage=1800, stale-while-revalidate=86400, stale-if-error=31536000",
      "cloudflare-cdn-cache-control":
        "public, max-age=1800, stale-while-revalidate=86400, stale-if-error=31536000",
      etag: '"testbuild-merges"',
      "last-modified": "Mon, 07 Sep 2026 12:00:00 GMT",
    });
  });

  it("omits validators when the backend does not return them", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => merges,
    });

    await GET({ fetch, setHeaders });

    const headers = setHeaders.mock.calls[0][0];
    expect(headers).not.toHaveProperty("etag");
    expect(headers).not.toHaveProperty("last-modified");
  });

  it("returns 503 when the backend request fails", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false });

    await expect(GET({ fetch, setHeaders: vi.fn() })).rejects.toThrow();
  });
});
