import { afterEach, describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";
import { GET as boardGET } from "../data/activity/+server.js";
import {
  AGENT_ACTIVITY_CACHE_CONTROL,
  cloudflareCacheHeaders,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

describe("documented public agent activity proxy", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shares the board proxy, upstream URL, timeout, and cache contract", async () => {
    expect(GET).toBe(boardGET);
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const payload = { daily: [{ cost_source: "mixed" }], now: {} };
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        headers: { etag: '"activity"' },
      }),
    );
    const setHeaders = vi.fn();

    const response = await GET({ fetch, setHeaders });

    expect(timeout).toHaveBeenCalledWith(10_000);
    expect(fetch).toHaveBeenCalledWith(
      `${process.env.API_BASE || "http://localhost:8000"}/api/agents/public/activity`,
      { signal: expect.any(AbortSignal) },
    );
    expect(await response.json()).toEqual(payload);
    expect(setHeaders).toHaveBeenCalledWith({
      ...cloudflareCacheHeaders(AGENT_ACTIVITY_CACHE_CONTROL),
      etag: versionedEtag('"activity"'),
    });
  });

  it("does not cache failed upstream responses", async () => {
    const setHeaders = vi.fn();
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 500 }));

    await expect(GET({ fetch, setHeaders })).rejects.toMatchObject({
      status: 503,
    });
    expect(setHeaders).not.toHaveBeenCalled();
  });
});
