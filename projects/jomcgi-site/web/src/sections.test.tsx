import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { GROUPS, ORIGIN, SECTIONS, hrefFor, sectionsIn } from "./sections";

describe("URL contract", () => {
  it("gives every section one top-level prefix", () => {
    for (const section of SECTIONS) {
      expect(section.path).toMatch(/^\/[a-z]+$/);
    }
    const paths = SECTIONS.map((section) => section.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("keeps the four nav groups, each with at least one section", () => {
    expect(GROUPS).toEqual(["Writing", "Apps", "Factory", "About"]);
    for (const group of GROUPS) {
      expect(sectionsIn(group).length).toBeGreaterThan(0);
    }
  });

  it("links a section that is not live to its current page on jomcgi.dev", () => {
    for (const section of SECTIONS) {
      expect(hrefFor(section)).toBe(
        section.live ? section.path : `${ORIGIN}${section.legacy}`,
      );
    }
  });

  it("paints the same links in the static HTML before JavaScript runs", () => {
    for (const page of ["index.html", "404.html"]) {
      const html = readFileSync(page, "utf8");
      const links = [...html.matchAll(/<nav[\s\S]*?<\/nav>/g)]
        .flatMap(([nav]) => [...nav.matchAll(/href="([^"]+)"/g)])
        .map((match) => match[1]);
      expect(links).toEqual(SECTIONS.map(hrefFor));
    }
  });
});
