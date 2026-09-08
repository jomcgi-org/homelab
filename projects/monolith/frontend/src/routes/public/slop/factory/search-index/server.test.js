import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const makeHeaders = (values = {}) => ({
  get(name) {
    return values[name.toLowerCase()] ?? null;
  },
});

describe("/public/slop/factory/search-index GET", () => {
  it("proxies the compact public search index unchanged", async () => {
    const index = {
      generated_at: "2026-09-08T06:00:00Z",
      states: ["verified"],
      entities: ["embervm"],
      notes: [["fact", "Fact", 0, 0]],
    };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => index,
    });

    const response = await GET({ fetch, setHeaders: vi.fn() });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/knowledge\/public\/search-index$/,
    );
    expect(await response.json()).toEqual(index);
  });

  it("versions upstream validators and preserves last-modified", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({ etag: '"index"', "last-modified": "today" }),
      json: async () => ({ notes: [] }),
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control":
        "public, max-age=300, s-maxage=300, stale-while-revalidate=86400",
      "cloudflare-cdn-cache-control":
        "public, max-age=300, stale-while-revalidate=86400",
      etag: '"testbuild-index"',
      "last-modified": "today",
    });
  });

  it("returns 503 when the backend fails", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false });
    await expect(GET({ fetch, setHeaders: vi.fn() })).rejects.toThrow();
  });
});
