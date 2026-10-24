import { afterEach, describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";
import { GET as activityGET } from "../data/activity/+server.js";
import { createPublicSnapshotCache } from "../../../../../lib/server/public-snapshots.js";
import { readPublicSnapshot } from "../../../../../lib/server/public-snapshots.js";

vi.mock(
  "../../../../../lib/server/public-snapshots.js",
  async (importOriginal) => ({
    ...(await importOriginal()),
    readPublicSnapshot: vi.fn(),
  }),
);

describe("documented public agent activity proxy", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shares the activity proxy and serves its anonymous snapshot lease", async () => {
    expect(GET).toBe(activityGET);
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const payload = { daily: [{ cost_source: "mixed" }], now: {} };
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        headers: {
          etag: '\"activity\"',
          "cache-control": "public, max-age=60, s-maxage=60",
        },
      }),
    );
    const cache = createPublicSnapshotCache({
      fetcher,
      base: "http://public-backend",
      now: () => 0,
    });
    readPublicSnapshot.mockImplementation((name) => cache.read(name));
    const setHeaders = vi.fn();
    const callerFetch = vi.fn();

    const response = await GET({ fetch: callerFetch, setHeaders });

    expect(timeout).toHaveBeenCalledWith(10_000);
    expect(fetcher).toHaveBeenCalledWith(
      "http://public-backend/api/agents/public/activity",
      {
        credentials: "omit",
        redirect: "error",
        headers: { accept: "application/json" },
        signal: expect.any(AbortSignal),
      },
    );
    expect(callerFetch).not.toHaveBeenCalled();
    expect(await response.json()).toEqual(payload);
    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": "public, max-age=60, s-maxage=60, must-revalidate",
        "cloudflare-cdn-cache-control": "public, max-age=60, must-revalidate",
      }),
    );
  });

  it("does not cache failed upstream responses", async () => {
    const setHeaders = vi.fn();
    readPublicSnapshot.mockRejectedValue(new Error("backend unavailable"));
    await expect(GET({ setHeaders })).rejects.toMatchObject({ status: 503 });
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "no-store",
      "cloudflare-cdn-cache-control": "no-store",
    });
  });
});
