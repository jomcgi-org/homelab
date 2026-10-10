import { describe, expect, it } from "vitest";
import {
  AGENT_ACTIVITY_CACHE_CONTROL,
  CAMPSITES_SNAPSHOT_CACHE_CONTROL,
  cloudflareCacheHeaders,
  boundedCacheHeaders,
  DOCS_CACHE_CONTROL,
  DR_JOBS_LISTINGS_CACHE_CONTROL,
  GRIMOIRE_READ_CACHE_CONTROL,
  HIKES_WALKS_CACHE_CONTROL,
  MERGES_CACHE_CONTROL,
  NOTES_PAGE_CACHE_CONTROL,
  PAGE_CACHE_CONTROL,
  SEARCH_INDEX_CACHE_CONTROL,
  SHIPS_HEAT_CACHE_CONTROL,
  SHIPS_SNAPSHOT_CACHE_CONTROL,
  SHIPS_TRACK_CACHE_CONTROL,
  STARS_HISTORY_CACHE_CONTROL,
  STARS_SITES_CACHE_CONTROL,
  STATS_CACHE_CONTROL,
  TRIPS_CACHE_CONTROL,
} from "./cache-headers.js";

const SHARED_POLICIES = [
  [AGENT_ACTIVITY_CACHE_CONTROL, 300],
  [PAGE_CACHE_CONTROL, 60],
  [STATS_CACHE_CONTROL, 60],
  [SEARCH_INDEX_CACHE_CONTROL, 300],
  [NOTES_PAGE_CACHE_CONTROL, 3_600],
  [DOCS_CACHE_CONTROL, 3_600],
  [SHIPS_SNAPSHOT_CACHE_CONTROL, 120],
  [SHIPS_TRACK_CACHE_CONTROL, 60],
  [SHIPS_HEAT_CACHE_CONTROL, 300],
  [HIKES_WALKS_CACHE_CONTROL, 1_800],
  [MERGES_CACHE_CONTROL, 1_800],
  [DR_JOBS_LISTINGS_CACHE_CONTROL, 1_800],
  [STARS_SITES_CACHE_CONTROL, 1_800],
  [STARS_HISTORY_CACHE_CONTROL, 31_536_000],
  [TRIPS_CACHE_CONTROL, 300],
  [CAMPSITES_SNAPSHOT_CACHE_CONTROL, 60],
  [GRIMOIRE_READ_CACHE_CONTROL, 3_600],
];

describe("boundedCacheHeaders", () => {
  it.each([
    "no-store",
    "no-cache",
    "private",
    "",
    "public, s-maxage=wat",
    "public, s-maxage=0",
  ])(
    "fails closed for unavailable or restricted upstream policies: %s",
    (policy) => {
      expect(
        boundedCacheHeaders(
          NOTES_PAGE_CACHE_CONTROL,
          new Headers({ "cache-control": policy }),
        ),
      ).toEqual({
        "cache-control": "no-store",
        "cloudflare-cdn-cache-control": "no-store",
      });
    },
  );

  it("drops stale directives absent at the origin and bounds browsers", () => {
    const headers = boundedCacheHeaders(
      NOTES_PAGE_CACHE_CONTROL,
      new Headers({
        "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
      }),
    );
    expect(headers).toEqual({
      "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
      "cloudflare-cdn-cache-control": "public, max-age=13, must-revalidate",
    });
  });

  it("uses the stricter default for every duration and explicit zero browser age", () => {
    const headers = boundedCacheHeaders(
      "public, max-age=2, s-maxage=5, stale-while-revalidate=7",
      new Headers({
        "cache-control":
          "public, max-age=20, s-maxage=30, stale-while-revalidate=40, stale-if-error=50",
      }),
    );
    expect(headers["cache-control"]).toBe(
      "public, max-age=2, s-maxage=5, stale-while-revalidate=7",
    );
    expect(
      boundedCacheHeaders(
        NOTES_PAGE_CACHE_CONTROL,
        new Headers({
          "cache-control": "public, s-maxage=13",
        }),
      )["cache-control"],
    ).toBe("public, max-age=0, s-maxage=13");
  });

  it("consumes upstream age without restarting a lease", () => {
    const date = Date.UTC(2024, 5, 1, 12);
    const upstream = new Headers({
      "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
      date: new Date(date).toUTCString(),
      age: "8",
    });
    const headers = boundedCacheHeaders(
      NOTES_PAGE_CACHE_CONTROL,
      upstream,
      date + 5_000,
    );
    expect(headers["cache-control"]).toBe(
      "public, max-age=5, s-maxage=5, must-revalidate",
    );
    expect(headers.date).toBe(upstream.get("date"));
    expect(headers.age).toBe("8");
    expect(
      boundedCacheHeaders(NOTES_PAGE_CACHE_CONTROL, upstream, date + 13_000)[
        "cache-control"
      ],
    ).toBe("no-store");
  });

  it("never increases fresh plus stale lifetimes", () => {
    for (const ttl of [1, 13, 300, 7200]) {
      for (const stale of [0, 5, 86400]) {
        const policy = `public, max-age=${ttl}, s-maxage=${ttl}, stale-while-revalidate=${stale}, stale-if-error=${stale}`;
        const headers = boundedCacheHeaders(
          NOTES_PAGE_CACHE_CONTROL,
          new Headers({ "cache-control": policy }),
        );
        const parsed = Object.fromEntries(
          headers["cache-control"]
            .split(", ")
            .filter((part) => part.includes("="))
            .map((part) => part.split("=")),
        );
        expect(Number(parsed["max-age"])).toBeLessThanOrEqual(ttl);
        expect(Number(parsed["s-maxage"])).toBeLessThanOrEqual(ttl);
        for (const name of ["stale-while-revalidate", "stale-if-error"]) {
          expect(
            Number(parsed["s-maxage"]) + Number(parsed[name] || 0),
          ).toBeLessThanOrEqual(ttl + stale);
          expect(
            Number(parsed["max-age"]) + Number(parsed[name] || 0),
          ).toBeLessThanOrEqual(ttl + stale);
        }
      }
    }
  });
});

describe("cloudflareCacheHeaders", () => {
  it.each(SHARED_POLICIES)(
    "moves shared max age into the Cloudflare-only policy",
    (cacheControl, edgeTtl) => {
      const headers = cloudflareCacheHeaders(cacheControl);
      const cloudflareDirectives =
        headers["cloudflare-cdn-cache-control"].split(", ");

      expect(headers["cache-control"]).toBe(cacheControl);
      expect(cloudflareDirectives).toContain(`max-age=${edgeTtl}`);
      expect(
        cloudflareDirectives.some((part) => part.startsWith("s-maxage=")),
      ).toBe(false);
    },
  );

  it("keeps the browser max age out of Cloudflare's policy", () => {
    const headers = cloudflareCacheHeaders(TRIPS_CACHE_CONTROL);
    const cloudflareDirectives =
      headers["cloudflare-cdn-cache-control"].split(", ");

    expect(headers["cache-control"]).toContain("max-age=60");
    expect(cloudflareDirectives).not.toContain("max-age=60");
    expect(cloudflareDirectives).toContain("max-age=300");
  });

  it("rejects a policy without a shared TTL", () => {
    expect(() => cloudflareCacheHeaders("public, max-age=60")).toThrow(
      "Cloudflare cache policy requires s-maxage",
    );
  });
});
