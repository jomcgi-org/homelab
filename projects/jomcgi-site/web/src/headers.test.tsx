import { readFileSync } from "node:fs";
import { robots } from "../machine-readable.mjs";
import { describe, expect, it } from "vitest";

const headers = readFileSync("public/_headers", "utf8");
const csp = headers.match(/Content-Security-Policy: (.+)/)?.[1] ?? "";

describe("static hosting headers", () => {
  it("allows the map host and workers, without data URLs in connect-src", () => {
    expect(csp.match(/connect-src ([^;]+)/)?.[1]).toContain(
      "https://tiles.openfreemap.org",
    );
    expect(csp.match(/img-src ([^;]+)/)?.[1]).toContain(
      "https://tiles.openfreemap.org",
    );
    expect(csp.match(/connect-src ([^;]+)/)?.[1]).not.toContain("data:");
    expect(csp).toContain("worker-src 'self' blob:");
    expect(headers).toContain("geolocation=(self)");
    expect(readFileSync("public/_redirects", "utf8")).toContain(
      "/app/hikes /hikes 301\n/app/hikes/* /hikes 301",
    );
  });
  it("sends a CSP without inline or eval exceptions", () => {
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("form-action 'none'");
    expect(csp).not.toMatch(/unsafe-inline|unsafe-eval/);
  });

  it("caches hashed assets forever", () => {
    expect(headers).toMatch(
      /\/assets\/\*\n\s+Cache-Control: public, max-age=31536000, immutable/,
    );
  });

  it("caches content-named section data forever, but not latest.json", () => {
    expect(headers).toMatch(
      /\/data\/:section\/v\/\*\n\s+Cache-Control: public, max-age=31536000, immutable/,
    );
    // latest.json keeps the default revalidation, so new data shows at once:
    // no rule path names it.
    const paths = headers.split("\n").filter((line) => /^[/h]/.test(line));
    expect(paths.filter((path) => path.includes("latest"))).toEqual([]);
  });

  it("keeps the preview host out of search results without hiding the rule", () => {
    expect(headers).toMatch(
      /https:\/\/next\.jomcgi\.dev\/\*\n\s+X-Robots-Tag: noindex/,
    );
    // A Disallow would stop crawlers from fetching pages and seeing noindex.
    expect(robots()).toContain("Allow: /\nDisallow: /slop");
    expect(robots()).not.toMatch(/Disallow: \/(?:\s|$)/);
  });

  it("sends noindex only for the preview host and the drafts note", () => {
    const rules = headers.split(/\n(?=\S)/);
    for (const rule of rules.filter((r) => r.includes("X-Robots-Tag"))) {
      expect(["https://next.jomcgi.dev/*", "/slop"]).toContain(
        rule.split("\n")[0],
      );
    }
  });
});
