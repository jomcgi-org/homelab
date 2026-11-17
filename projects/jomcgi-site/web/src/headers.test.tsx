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

  it("keeps the preview host out of search engines", () => {
    expect(readFileSync("public/robots.txt", "utf8")).toMatch(
      /Disallow: \/\s*$/,
    );
  });
});
