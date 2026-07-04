import { describe, it, expect } from "vitest";
import { stack, LINK_LABELS } from "./homepage-stack.js";

const systems = stack
  .filter((l) => l.kind === "projects")
  .flatMap((l) => l.items);

describe("homepage stack config", () => {
  it("has the five strata in order", () => {
    expect(stack.map((l) => l.id)).toEqual([
      "apps",
      "systems",
      "platform",
      "compute",
      "metal",
    ]);
  });

  it("has unique system ids", () => {
    const ids = systems.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every system has the required story fields and at least one link", () => {
    const linkKeys = LINK_LABELS.map(([key]) => key);
    for (const s of systems) {
      expect(s.name, s.id).toBeTruthy();
      expect(s.blurb, s.id).toBeTruthy();
      expect(s.engineering, s.id).toBeTruthy();
      expect(s.tags?.length, s.id).toBeGreaterThan(0);
      const keys = Object.keys(s.links ?? {});
      expect(keys.length, s.id).toBeGreaterThan(0);
      for (const k of keys) expect(linkKeys, `${s.id}.${k}`).toContain(k);
    }
  });

  it("live and docs links are same-origin paths, code links point at the repo", () => {
    for (const s of systems) {
      if (s.links.live) expect(s.links.live, s.id).toMatch(/^\//);
      if (s.links.docs) expect(s.links.docs, s.id).toMatch(/^\/docs/);
      if (s.links.code) {
        expect(s.links.code, s.id).toMatch(
          /^https:\/\/github\.com\/jomcgi\/homelab(\/tree\/main\/.+)?$/,
        );
      }
    }
  });

  it("apps strip items are all live same-origin links", () => {
    const apps = stack.find((l) => l.id === "apps");
    expect(apps.kind).toBe("strip");
    for (const item of apps.items) {
      expect(item.name, "apps").toBeTruthy();
      expect(item.href, item.name).toMatch(/^\//);
    }
  });

  it("strip items have names", () => {
    for (const layer of stack.filter((l) => l.kind === "strip")) {
      for (const item of layer.items) expect(item.name, layer.id).toBeTruthy();
    }
  });
});
