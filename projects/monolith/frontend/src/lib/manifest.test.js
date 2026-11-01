import { describe, expect, it } from "vitest";
import { manifest, manifestResponse } from "./manifest.js";

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

  it("serves the manifest media type with a short cache", async () => {
    const res = manifestResponse("jomcgi.dev", "jomcgi");
    expect(res.headers.get("content-type")).toBe("application/manifest+json");
    expect(res.headers.get("cache-control")).toBe("public, max-age=600");
    expect((await res.json()).short_name).toBe("jomcgi");
  });
});
