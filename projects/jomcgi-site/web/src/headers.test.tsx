import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const headers = readFileSync("public/_headers", "utf8");
const csp = headers.match(/Content-Security-Policy: (.+)/)?.[1] ?? "";

describe("static hosting headers", () => {
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
    expect(readFileSync("public/robots.txt", "utf8")).not.toMatch(/Disallow/);
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
