import { describe, expect, it } from "vitest";
import { GET } from "./+server.js";

describe("/public/robots.txt", () => {
  it("disallows crawlers from accessing /slop/", async () => {
    const body = await GET().text();

    expect(body).toContain("Disallow: /slop/");
  });

  it("includes the sitemap reference", async () => {
    const body = await GET().text();

    expect(body).toContain("Sitemap: https://jomcgi.dev/sitemap.xml");
  });
});
