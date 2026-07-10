import { describe, it, expect } from "vitest";
import {
  emptyConstellation,
  withTouched,
  withEgo,
} from "./constellation-state.js";

const strahd = {
  id: 7,
  title: "Strahd von Zarovich",
  kind: "entity",
  entity_type: "npc",
};
const ireena = {
  id: 8,
  title: "Ireena Kolyana",
  kind: "entity",
  entity_type: "npc",
};
const chunk = { id: 1, title: "Chapter 1", kind: "chunk" };

describe("emptyConstellation", () => {
  it("starts with no nodes, no edges", () => {
    const s = emptyConstellation();
    expect(s.nodes).toEqual([]);
    expect(s.edges).toEqual([]);
    expect(s.ids.size).toBe(0);
  });
});

describe("withTouched", () => {
  it("ignores chunk items", () => {
    const s = withTouched(emptyConstellation(), chunk);
    expect(s.nodes).toEqual([]);
  });

  it("adds a new entity node", () => {
    const s = withTouched(emptyConstellation(), strahd);
    expect(s.nodes).toEqual([
      { id: 7, name: "Strahd von Zarovich", entity_type: "npc" },
    ]);
    expect(s.ids.has(7)).toBe(true);
  });

  it("ignores duplicate entity ids", () => {
    let s = withTouched(emptyConstellation(), strahd);
    s = withTouched(s, { ...strahd, title: "Strahd (renamed)" });
    expect(s.nodes.length).toBe(1);
    expect(s.nodes[0].name).toBe("Strahd von Zarovich");
  });

  it("returns the same reference for a no-op fold", () => {
    const s0 = emptyConstellation();
    const s1 = withTouched(s0, chunk);
    expect(s1).toBe(s0);
  });

  it("tolerates malformed items (missing id, null)", () => {
    const s0 = emptyConstellation();
    expect(withTouched(s0, null)).toBe(s0);
    expect(withTouched(s0, { kind: "entity" })).toBe(s0);
  });
});

describe("withEgo", () => {
  it("drops edges to non-session entities", () => {
    let s = withTouched(emptyConstellation(), strahd);
    s = withEgo(s, 7, { edges: [{ from: 7, to: 999 }] });
    expect(s.edges).toEqual([]);
  });

  it("keeps edges where both endpoints are session nodes", () => {
    let s = withTouched(emptyConstellation(), strahd);
    s = withTouched(s, ireena);
    s = withEgo(s, 7, { edges: [{ from: 7, to: 8 }] });
    expect(s.edges).toEqual([{ from: 7, to: 8 }]);
  });

  it("dedupes an undirected pair reported both ways", () => {
    let s = withTouched(emptyConstellation(), strahd);
    s = withTouched(s, ireena);
    s = withEgo(s, 7, { edges: [{ from: 7, to: 8 }] });
    s = withEgo(s, 8, { edges: [{ from: 8, to: 7 }] });
    expect(s.edges.length).toBe(1);
  });

  it("drops self-loop edges", () => {
    let s = withTouched(emptyConstellation(), strahd);
    s = withEgo(s, 7, { edges: [{ from: 7, to: 7 }] });
    expect(s.edges).toEqual([]);
  });

  it("recomputes edges when a later node makes an earlier ego's edge valid", () => {
    // Strahd's ego names a relationship to Ireena before Ireena has arrived
    // as a session node: the edge is correctly dropped at that point.
    let s = withTouched(emptyConstellation(), strahd);
    s = withEgo(s, 7, { edges: [{ from: 7, to: 8 }] });
    expect(s.edges).toEqual([]);

    // Ireena arrives, then her own ego fetch resolves (even with no edges of
    // her own): the recompute over all stored egos picks up Strahd's
    // earlier-reported edge now that both ends are session nodes.
    s = withTouched(s, ireena);
    s = withEgo(s, 8, { edges: [] });
    expect(s.edges).toEqual([{ from: 7, to: 8 }]);
  });

  it("tolerates a malformed or empty ego payload", () => {
    let s = withTouched(emptyConstellation(), strahd);
    expect(() => withEgo(s, 7, null)).not.toThrow();
    expect(withEgo(s, 7, null).edges).toEqual([]);
    expect(() => withEgo(s, 7, {})).not.toThrow();
    expect(withEgo(s, 7, {}).edges).toEqual([]);
    expect(() => withEgo(s, 7, { edges: [null, undefined, {}] })).not.toThrow();
  });
});
