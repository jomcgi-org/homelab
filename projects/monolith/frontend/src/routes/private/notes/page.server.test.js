import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

describe("/notes load", () => {
  it("fetches the graph and sets a 1h s-maxage cache-control header", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => ({ nodes: [], edges: [], indexed_at: null }),
    });

    const result = await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": expect.stringContaining("s-maxage=3600"),
      }),
    );
    expect(result.graph).toEqual({ nodes: [], edges: [], indexed_at: null });
  });

  it("versions the API ETag with the build version and forwards Last-Modified", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"abc-3"',
        "Last-Modified": "Mon, 27 Apr 2026 12:00:00 GMT",
      }),
      json: async () => ({ nodes: [], edges: [], indexed_at: null }),
    });

    await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        // Build version (testbuild, from the $app/environment stub) is spliced
        // inside the quotes so a layout-only deploy busts the page validator.
        etag: '"testbuild-abc-3"',
        "last-modified": "Mon, 27 Apr 2026 12:00:00 GMT",
      }),
    );
  });

  it("omits ETag header when the API does not return one", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => ({ nodes: [], edges: [], indexed_at: null }),
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
