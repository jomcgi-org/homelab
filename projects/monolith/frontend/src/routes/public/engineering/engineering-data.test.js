import { describe, it, expect } from "vitest";
import {
  intro,
  marqueeItems,
  categories,
  projects,
} from "./engineering-data.js";
import { diagramIds } from "./diagrams/registry-ids.js";

describe("engineering-data", () => {
  it("has hero content", () => {
    expect(intro.title).toBeTruthy();
    expect(intro.lede).toBeTruthy();
    expect(marqueeItems.length).toBeGreaterThan(5);
  });

  it("every project has the required fields", () => {
    expect(projects.length).toBe(12);
    for (const p of projects) {
      expect(p.id, p.title).toMatch(/^[a-z0-9-]+$/);
      expect(p.title).toBeTruthy();
      expect(
        categories[p.category],
        `unknown category on ${p.id}`,
      ).toBeTruthy();
      expect(p.oneLiner).toBeTruthy();
      expect(p.motivation).toBeTruthy();
      expect(p.facts.length).toBeGreaterThanOrEqual(3);
      for (const f of p.facts) {
        expect(f.k).toBeTruthy();
        expect(f.v).toBeTruthy();
      }
      for (const l of p.links ?? []) {
        expect(l.label).toBeTruthy();
        expect(l.href).toMatch(/^(https:\/\/|\/)/);
      }
    }
  });

  it("project ids are unique (they become DOM anchors)", () => {
    const ids = projects.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every project has a diagram registered", () => {
    for (const p of projects) {
      expect(diagramIds, `missing diagram for ${p.id}`).toContain(p.id);
    }
  });

  it("copy contains no em-dashes", () => {
    const blob = JSON.stringify({ intro, marqueeItems, projects });
    expect(blob).not.toContain("—");
  });
});
