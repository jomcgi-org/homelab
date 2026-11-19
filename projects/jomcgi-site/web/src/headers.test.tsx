import { readFileSync } from "node:fs";
import { robots } from "../machine-readable.mjs";
import { describe, expect, it } from "vitest";

const headers = readFileSync("public/_headers", "utf8");
const rule = (path: string) =>
  headers.split(/\n(?=\S)/).find((r) => r.split("\n")[0] === path) ?? "";
const policy = (block: string) =>
  block.match(/^ {2}Content-Security-Policy: (.+)/m)?.[1] ?? "";
const csp = policy(rule("/*"));
const hikes = rule("/hikes");
const hikesCsp = policy(hikes);

describe("static hosting headers", () => {
  it("permits the analytics script and collector on trips pages", () => {
    for (const path of ["/trips", "/trips/*"]) {
      const directives = policy(rule(path)).split("; ");
      expect(directives.filter((d) => d.startsWith("script-src "))).toEqual([
        "script-src 'self' https://static.cloudflareinsights.com/beacon.min.js",
      ]);
      expect(
        directives.find((d) => d.startsWith("connect-src ")),
      ).toContain("https://cloudflareinsights.com");
    }
  });
  it("permits the analytics script and collector on site, hikes and stars pages", () => {
    for (const path of ["/*", "/hikes", "/stars"]) {
      const directives = policy(rule(path)).split("; ");
      // Include sources in every directive so a new host in img-src,
      // worker-src or another directive also fails the origin check.
      const origins = new Set(
        directives.flatMap((directive) =>
          directive
            .split(" ")
            .filter((source) => source.includes("://"))
            .map((source) => new URL(source).origin),
        ),
      );
      expect(origins).toEqual(
        new Set([
          "https://static.cloudflareinsights.com",
          "https://cloudflareinsights.com",
          "https://data.jomcgi.dev",
          ...(path === "/*" ? [] : ["https://tiles.openfreemap.org"]),
        ]),
      );
      // The beacon path is stricter than its origin, and it meets HOME-382
      // criterion 2 (decision: https://factory-plan.jomcgi.dev/factory/issue/HOME-382#comment-3949).
      expect(directives.filter((d) => d.startsWith("script-src "))).toEqual([
        "script-src 'self' https://static.cloudflareinsights.com/beacon.min.js",
      ]);
      expect(directives.filter((d) => d.startsWith("connect-src "))).toEqual([
        "connect-src 'self' https://cloudflareinsights.com https://data.jomcgi.dev" +
          (path === "/*" ? "" : " https://tiles.openfreemap.org"),
      ]);
    }
  });
  it("gives the hikes map tile access, workers and geolocation", () => {
    expect(csp).not.toMatch(/openfreemap|blob:/);
    expect(rule("/*")).toContain("geolocation=()");
    expect(hikes).toContain(
      "! Content-Security-Policy\n  ! Permissions-Policy",
    );
    for (const src of ["connect-src", "img-src"])
      expect(hikesCsp.match(new RegExp(src + " ([^;]+)"))?.[1]).toContain(
        "https://tiles.openfreemap.org",
      );
    expect(hikesCsp.match(/connect-src ([^;]+)/)?.[1]).not.toContain("data:");
    // Apart from the map, /hikes keeps the site policy.
    expect(
      hikesCsp
        .replaceAll(" https://tiles.openfreemap.org", "")
        .replace(" worker-src 'self' blob:;", ""),
    ).toBe(csp);
    expect(hikes).toContain("geolocation=(self)");
    expect(readFileSync("public/_redirects", "utf8")).toContain(
      "/app/hikes /hikes 301\n/app/hikes/* /hikes 301",
    );
  });
  it.each(["/trips", "/trips/*"])(
    "allows Trips photos and maps on %s",
    (path) => {
      const trips = policy(rule(path));
      expect(rule(path)).toContain("! Content-Security-Policy");
      expect(trips.match(/img-src ([^;]+)/)?.[1]).toContain(
        "https://factory-data.jomcgi.dev",
      );
      expect(trips.match(/connect-src ([^;]+)/)?.[1]).toContain(
        "https://tiles.openfreemap.org",
      );
      expect(trips).toContain("worker-src 'self' blob:");
    },
  );
  it("lets browsers revalidate the /app/hikes 301, so a rollback reaches the hub", () => {
    for (const path of ["/app/hikes", "/app/hikes/*"])
      expect(rule(path)).toContain(
        "Cache-Control: public, max-age=0, must-revalidate",
      );
  });
  it("sends a CSP without inline or eval exceptions", () => {
    for (const value of [csp, hikesCsp, policy(rule("/stars"))]) {
      expect(value).toContain("default-src 'self'");
      expect(value).toContain("frame-ancestors 'none'");
      expect(value).toContain("form-action 'none'");
      expect(value).not.toMatch(/unsafe-inline|unsafe-eval/);
    }
  });

  it("gives /stars the hikes map policy, no geolocation, a revalidated 301", () => {
    const stars = rule("/stars");
    expect(stars).toContain("! Content-Security-Policy");
    expect(policy(stars)).toBe(hikesCsp);
    expect(stars).not.toContain("Permissions-Policy");
    expect(readFileSync("public/_redirects", "utf8")).toContain(
      "/app/stars /stars 301\n/app/stars/* /stars 301",
    );
    for (const path of ["/app/stars", "/app/stars/*"])
      expect(rule(path)).toContain(
        "Cache-Control: public, max-age=0, must-revalidate",
      );
  });

  it("adds tile connections only to the MapLibre worker asset policy", () => {
    const worker = rule("/assets/maplibre-gl-worker-*.js");
    expect(worker).toContain("! Content-Security-Policy");
    // The worker runs no analytics: it may fetch only same-origin scripts,
    // site data and tiles, whatever the page policy grants.
    expect(policy(worker)).toBe(
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' https://data.jomcgi.dev https://tiles.openfreemap.org; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'",
    );
    expect(policy(worker)).not.toContain("cloudflareinsights");
    expect(rule("/assets/*")).not.toContain("Content-Security-Policy");
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
