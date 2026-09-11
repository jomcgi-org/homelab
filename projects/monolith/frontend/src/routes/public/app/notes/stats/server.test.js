import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const stats = {
  cluster: { nodes: 4 },
  gpu: { utilization_pct: 50.0 },
};

describe("/public/app/notes/stats GET", () => {
  it("proxies the observability stats endpoint unchanged", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => stats,
    });

    const response = await GET({ fetch, setHeaders });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/home\/observability\/stats$/,
    );
    expect(await response.json()).toEqual(stats);
  });

  it("sets the shared browser and Cloudflare cache policies", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => stats,
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control":
        "public, max-age=0, s-maxage=60, stale-while-revalidate=86400, stale-if-error=31536000",
      "cloudflare-cdn-cache-control":
        "public, max-age=60, stale-while-revalidate=86400, stale-if-error=31536000",
    });
  });

  it("returns 503 without cache headers when the backend request fails", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false });

    await expect(GET({ fetch, setHeaders })).rejects.toThrow();
    expect(setHeaders).not.toHaveBeenCalled();
  });
});
