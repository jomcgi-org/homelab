import { describe, expect, it } from "vitest";
import {
  CAMPSITES_SNAPSHOT_CACHE_CONTROL,
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
  DR_JOBS_LISTINGS_CACHE_CONTROL,
  GRIMOIRE_READ_CACHE_CONTROL,
  HIKES_WALKS_CACHE_CONTROL,
  NOTES_PAGE_CACHE_CONTROL,
  PAGE_CACHE_CONTROL,
  SHIPS_HEAT_CACHE_CONTROL,
  SHIPS_SNAPSHOT_CACHE_CONTROL,
  SHIPS_TRACK_CACHE_CONTROL,
  STARS_HISTORY_CACHE_CONTROL,
  STARS_SITES_CACHE_CONTROL,
  TRIPS_CACHE_CONTROL,
} from "./cache-headers.js";

const SHARED_POLICIES = [
  [PAGE_CACHE_CONTROL, 60],
  [NOTES_PAGE_CACHE_CONTROL, 3_600],
  [DOCS_CACHE_CONTROL, 3_600],
  [SHIPS_SNAPSHOT_CACHE_CONTROL, 120],
  [SHIPS_TRACK_CACHE_CONTROL, 60],
  [SHIPS_HEAT_CACHE_CONTROL, 300],
  [HIKES_WALKS_CACHE_CONTROL, 1_800],
  [DR_JOBS_LISTINGS_CACHE_CONTROL, 1_800],
  [STARS_SITES_CACHE_CONTROL, 1_800],
  [STARS_HISTORY_CACHE_CONTROL, 31_536_000],
  [TRIPS_CACHE_CONTROL, 300],
  [CAMPSITES_SNAPSHOT_CACHE_CONTROL, 60],
  [GRIMOIRE_READ_CACHE_CONTROL, 3_600],
];

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
