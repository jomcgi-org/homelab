import { describe, it, expect } from "vitest";
import { stack } from "./homepage-stack.js";

const projects = stack
  .filter((l) => l.kind === "projects")
  .flatMap((l) => l.items);

describe("homepage stack config", () => {
  it("has the four strata in order", () => {
    expect(stack.map((l) => l.id)).toEqual([
      "apps",
      "platform",
      "compute",
      "metal",
    ]);
  });

  it("has unique project ids", () => {
    const ids = projects.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every project has the required story fields", () => {
    for (const p of projects) {
      expect(p.name, p.id).toBeTruthy();
      expect(p.blurb, p.id).toBeTruthy();
      expect(p.engineering, p.id).toBeTruthy();
      expect(p.tags?.length, p.id).toBeGreaterThan(0);
      expect(p.links?.readme, p.id).toMatch(
        /^https:\/\/github\.com\/jomcgi\/homelab\/tree\/main\//,
      );
    }
  });

  it("live links are same-origin paths", () => {
    for (const p of projects) {
      if (p.links.live) expect(p.links.live, p.id).toMatch(/^\//);
    }
  });

  it("strip items have names", () => {
    for (const layer of stack.filter((l) => l.kind === "strip")) {
      for (const item of layer.items) expect(item.name, layer.id).toBeTruthy();
    }
  });
});
