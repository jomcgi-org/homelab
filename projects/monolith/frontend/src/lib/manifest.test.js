import { describe, expect, it } from "vitest";
import { manifest, manifestResponse } from "./manifest.js";
import { GET as publicManifest } from "../routes/public/manifest.webmanifest/+server.js";

describe("web app manifest", () => {
  it("names the installed app per host and starts at the host root", () => {
    const m = manifest("private.jomcgi.dev", "dashboard");
    expect(m).toMatchObject({
      name: "private.jomcgi.dev",
      short_name: "dashboard",
      start_url: "/",
      display: "standalone",
    });
    expect(m.icons.map((icon) => icon.src)).toEqual([
      "/favicon.svg",
      "/apple-touch-icon.png",
    ]);
  });

  it("serves the manifest media type with the route's cache policy", async () => {
    const res = manifestResponse("jomcgi.dev", "jomcgi", {
      "cache-control": "private, max-age=1",
    });
    expect(res.headers.get("content-type")).toBe("application/manifest+json");
    expect(res.headers.get("cache-control")).toBe("private, max-age=1");
    expect((await res.json()).short_name).toBe("jomcgi");
  });

  it("shares the apex manifest at the edge under the cache contract", async () => {
    const res = publicManifest();
    expect(res.headers.get("cache-control")).toMatch(
      /^public, max-age=600, s-maxage=/,
    );
    expect(res.headers.get("cloudflare-cdn-cache-control")).toMatch(
      /^public, max-age=3600, stale-while-revalidate=/,
    );
    expect((await res.json()).short_name).toBe("jomcgi");
  });
});
