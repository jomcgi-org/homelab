import { describe, expect, it } from "vitest";
import { PAGES } from "./pages";
import { GROUPS, SECTIONS, built, hrefFor, sectionsIn } from "./sections";

describe("URL contract", () => {
  it("gives every section one top-level prefix", () => {
    for (const section of SECTIONS) {
      expect(section.path).toMatch(/^\/[a-z]+(?:-[a-z]+)*$/);
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

  it("links only live sections, since the hub pages of the others are retired", () => {
    // HOME-261: jomcgi.dev/* answers a retired hub URL with the 404 page, so
    // no page may link a section that this site does not serve.
    for (const section of SECTIONS) {
      if (section.live) expect(hrefFor(section)).toBe(section.path);
      else expect(() => hrefFor(section)).toThrow(/not live/);
    }
    for (const group of GROUPS) {
      for (const section of sectionsIn(group)) expect(section.live).toBe(true);
    }
  });

  it("builds a section's own entries only while it is live", () => {
    // The Ships page reads data it cannot get yet, so it ships no page
    // until HOME-411 sets it live.
    const ships = SECTIONS.find((section) => section.name === "Ships")!;
    expect(ships.entries).toEqual(["app/ships.html"]);
    expect(built("app/ships.html")).toBe(ships.live);
    for (const section of SECTIONS)
      for (const entry of section.entries ?? []) {
        expect(Object.keys(PAGES), entry).toContain(entry);
        expect(built(entry), entry).toBe(section.live);
      }
    const owned = SECTIONS.flatMap((section) => section.entries ?? []);
    for (const entry of Object.keys(PAGES))
      if (!owned.includes(entry)) expect(built(entry), entry).toBe(true);
  });
});
