import { describe, it, expect, vi } from "vitest";
import {
  radiusFor,
  graphFingerprint,
  layoutGraph,
} from "./knowledge-graph-layout.js";

const config = { baseRadius: 1.82, hubBoost: 0.325 };
const node = (id) => ({ id, title: id, type: "fact", degree: 1 });
function start(nodes, edges) {
  const frames = new Map();
  let next = 0;
  let clock = 0;
  const onTick = vi.fn();
  const layout = layoutGraph(nodes, edges, {
    width: 800,
    height: 600,
    config,
    colorFor: () => "ink",
    onTick,
    requestFrame: (callback) => {
      frames.set(++next, callback);
      return next;
    },
    cancelFrame: (id) => frames.delete(id),
    // One tick per batch makes cancellation deterministic.
    now: () => (clock += 9),
  });
  return {
    layout,
    frames,
    onTick,
    flush() {
      let count = 0;
      while (frames.size) {
        expect(++count).toBeLessThanOrEqual(120);
        const [id, callback] = frames.entries().next().value;
        frames.delete(id);
        callback();
      }
    },
  };
}

describe("client positioning", () => {
  it("retains radius scaling", () => {
    expect(radiusFor(0, config)).toBe(config.baseRadius);
    expect(radiusFor(3, config)).toBe(config.baseRadius + config.hubBoost * 2);
  });

  it.each(
    [[], [node("a")], [node("a"), node("b"), node("c")]].map((nodes) => ({
      nodes,
    })),
  )("positions empty, singleton and disconnected data: %j", ({ nodes }) => {
    const run = start(nodes, []);
    run.flush();
    expect(run.layout.nodes).toHaveLength(nodes.length);
    for (const n of run.layout.nodes) {
      expect(Number.isFinite(n.x) && Number.isFinite(n.y)).toBe(true);
    }
    expect(new Set(run.layout.nodes.map((n) => `${n.x},${n.y}`)).size).toBe(
      nodes.length,
    );
    run.layout.stop();
  });

  it("lays out new connected nodes after refresh without mutating payloads or edge IDs", () => {
    const nodes = [node("a"), node("b")].map(Object.freeze);
    const edges = [
      Object.freeze({ source: "a", target: "b", kind: "supports" }),
    ];
    const before = JSON.stringify({ nodes, edges });
    const first = start(nodes, edges);
    first.flush();
    first.layout.stop();
    const refreshed = start(
      [...nodes, node("c")],
      [...edges, { source: "b", target: "c" }],
    );
    refreshed.flush();
    const c = refreshed.layout.nodes.find((n) => n.id === "c");
    expect(Number.isFinite(c.x) && Number.isFinite(c.y)).toBe(true);
    expect(
      Math.hypot(
        c.x - refreshed.layout.nodes[1].x,
        c.y - refreshed.layout.nodes[1].y,
      ),
    ).toBeGreaterThan(3);
    expect(refreshed.layout.edges[0]).toEqual(edges[0]);
    expect(JSON.stringify({ nodes, edges })).toBe(before);
    expect(refreshed.onTick).toHaveBeenLastCalledWith(true);
    refreshed.layout.stop();
  });

  it("ignores dangling links and cached legacy positions", () => {
    const nodes = [node("a"), node("b")];
    const edges = [
      { source: "a", target: "b" },
      { source: "missing", target: "b" },
    ];
    const clean = start(nodes, edges);
    const cached = start(
      nodes.map((n) => ({ ...n, x: Infinity, y: -300 })),
      edges,
    );
    clean.flush();
    cached.flush();
    expect(cached.layout.nodes).toEqual(clean.layout.nodes);
    expect(clean.layout.edges).toEqual([
      { source: "a", target: "b", kind: null },
    ]);
    clean.layout.stop();
    cached.layout.stop();
  });

  it("cancels pending work and releases the simulation on stop", () => {
    const run = start([node("a"), node("b")], []);
    expect(run.frames.size).toBe(1);
    run.layout.stop();
    expect(run.frames.size).toBe(0);
    run.flush();
    expect(run.onTick).not.toHaveBeenCalled();
  });

  it("detects interior replacements, metadata and rewired edges", () => {
    const nodes = [node("a"), node("b"), node("c")];
    const edges = [{ source: "a", target: "b" }];
    const fp = graphFingerprint(nodes, edges);
    expect(graphFingerprint([nodes[0], node("d"), nodes[2]], edges)).not.toBe(
      fp,
    );
    expect(graphFingerprint(nodes, [{ source: "a", target: "c" }])).not.toBe(
      fp,
    );
    expect(
      graphFingerprint(
        nodes.map((n) => ({ ...n, title: "new" })),
        edges,
      ),
    ).not.toBe(fp);
    expect(
      graphFingerprint(
        nodes.map((n) => ({ ...n, x: 1, y: 2 })),
        edges,
      ),
    ).toBe(fp);
  });
});
