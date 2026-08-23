import { describe, expect, it, vi } from "vitest";
import {
  cloudflareCacheHeaders,
  PAGE_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { load } from "./+page.server.js";

describe("public homepage load", () => {
  it("sets separate browser and Cloudflare cache policies", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ nodes: 4 }),
    });

    const result = await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      cloudflareCacheHeaders(PAGE_CACHE_CONTROL),
    );
    expect(result.stats).toEqual({ nodes: 4 });
  });
});
