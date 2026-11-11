import { describe, expect, it } from "vitest";
import { GET } from "./+server.js";

describe("/scotland/marketplace.json", () => {
  it("serves the plugin catalog with an explicit edge policy", async () => {
    const res = GET();
    expect(res.headers.get("content-type")).toBe(
      "application/json; charset=utf-8",
    );
    expect(res.headers.get("cache-control")).toContain("s-maxage=3600");
    expect(res.headers.get("cloudflare-cdn-cache-control")).toContain(
      "max-age=3600",
    );
    const body = await res.json();
    expect(body.name).toBe("jomcgi");
    expect(body.owner.name).toBeTruthy();
  });

  it("points the plugin at its folder in the monorepo", async () => {
    const [plugin] = (await GET().json()).plugins;
    expect(plugin.name).toBe("scotland");
    // A url-hosted marketplace cannot resolve relative paths: the source has
    // to fetch on its own, and git-subdir checks out only the plugin folder.
    expect(plugin.source).toEqual({
      source: "git-subdir",
      url: "https://github.com/jomcgi-org/homelab.git",
      path: "projects/scotland-connector/plugin",
      ref: "main",
    });
  });
});
