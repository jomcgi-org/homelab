import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";
import {
  CAMPSITES_SNAPSHOT_CACHE_CONTROL,
  cloudflareCacheHeaders,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

describe("campsites unchanged server contract", () => {
  it("loads only through the server API with the existing timeout and cache validators", async () => {
    const snapshot = { parks: [], generated_at: "2026-10-19T12:00:00Z" };
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(snapshot), {
        headers: {
          etag: '"fixture"',
          "last-modified": "Mon, 19 Oct 2026 12:00:00 GMT",
        },
      }),
    );
    const setHeaders = vi.fn();
    const timeout = vi.spyOn(AbortSignal, "timeout");
    try {
      expect(await load({ fetch, setHeaders })).toEqual({ snapshot });
      expect(fetch.mock.calls[0][0]).toMatch(/\/api\/campsites\/snapshot$/);
      expect(timeout).toHaveBeenCalledWith(10_000);
      expect(setHeaders).toHaveBeenCalledWith({
        ...cloudflareCacheHeaders(CAMPSITES_SNAPSHOT_CACHE_CONTROL),
        etag: versionedEtag('"fixture"'),
        "last-modified": "Mon, 19 Oct 2026 12:00:00 GMT",
      });
    } finally {
      timeout.mockRestore();
    }
  });
  it("retains the existing unavailable error", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 503 }));
    await expect(load({ fetch, setHeaders: vi.fn() })).rejects.toMatchObject({
      status: 503,
      body: { message: "campsites data unavailable" },
    });
  });
});
