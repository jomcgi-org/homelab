import { describe, expect, it } from "vitest";
import { GET } from "./+server.js";

describe("private web app manifest", () => {
  it("names the console and stays out of shared caches", async () => {
    const res = GET();
    expect(res.headers.get("content-type")).toBe("application/manifest+json");
    expect(res.headers.get("cache-control")).toBe("private, max-age=600");
    expect(res.headers.get("cloudflare-cdn-cache-control")).toBeNull();
    expect(await res.json()).toMatchObject({
      name: "private.jomcgi.dev",
      short_name: "dashboard",
      start_url: "/",
    });
  });
});
