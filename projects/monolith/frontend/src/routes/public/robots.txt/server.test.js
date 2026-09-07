import { describe, expect, it } from "vitest";
import { GET } from "./+server.js";

function isDisallowed(body, path) {
  return body
    .split("\n")
    .filter((line) => line.startsWith("Disallow:"))
    .map((line) => line.slice("Disallow:".length).trim())
    .some((prefix) => prefix && path.startsWith(prefix));
}

describe("/public/robots.txt", () => {
  it("disallows the slop index and its descendants only", async () => {
    const body = await GET().text();

    expect(body).toContain("Disallow: /slop");
    expect(isDisallowed(body, "/slop")).toBe(true);
    expect(isDisallowed(body, "/slop/anything")).toBe(true);
    expect(isDisallowed(body, "/blog")).toBe(false);
  });

  it("includes the sitemap reference", async () => {
    const body = await GET().text();

    expect(body).toContain("Sitemap: https://jomcgi.dev/sitemap.xml");
  });
});
