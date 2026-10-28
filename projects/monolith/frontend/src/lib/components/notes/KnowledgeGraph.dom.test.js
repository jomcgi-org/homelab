// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { tick } from "svelte";
import { createClassComponent } from "svelte/legacy";
import KnowledgeGraph from "./KnowledgeGraph.svelte";
import * as layouts from "./knowledge-graph-layout.js";

let component;
let frames;
afterEach(async () => {
  component?.$destroy();
  component = undefined;
  await tick();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

async function setup(props = {}) {
  frames = new Map();
  let next = 0;
  vi.stubGlobal("requestAnimationFrame", (callback) => {
    frames.set(++next, callback);
    return next;
  });
  vi.stubGlobal("cancelAnimationFrame", (id) => frames.delete(id));
  let resize;
  const disconnect = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback) {
        resize = callback;
      }
      observe() {}
      disconnect = disconnect;
    },
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  const context = new Proxy(
    {
      measureText: vi.fn(() => ({ width: 50 })),
    },
    {
      get(target, key) {
        return (target[key] ??= vi.fn());
      },
    },
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context);
  vi.spyOn(
    HTMLCanvasElement.prototype,
    "getBoundingClientRect",
  ).mockReturnValue({ left: 0, top: 0, width: 800, height: 600 });
  const layout = vi.spyOn(layouts, "layoutGraph");
  const onNodeClick = vi.fn();
  const onZoom = vi.fn();
  component = createClassComponent({
    component: KnowledgeGraph,
    target: document.body,
    props: {
      nodes: [
        { id: "a", title: "Alpha", type: "fact", degree: 1 },
        { id: "b", title: "Beta", type: "fact", degree: 1 },
      ],
      edges: [
        { source: "a", target: "b" },
        { source: "missing", target: "b" },
      ],
      activeClusters: new Set(["fact"]),
      onNodeClick,
      onZoom,
      ...props,
    },
  });
  await tick();
  function flush() {
    let iterations = 0;
    while (frames.size) {
      expect(++iterations).toBeLessThan(500);
      const [id, callback] = frames.entries().next().value;
      frames.delete(id);
      callback();
    }
  }
  return {
    layout,
    context,
    onNodeClick,
    onZoom,
    flush,
    resize: () => resize(),
    disconnect,
  };
}

it("mounts the shared canvas with coordinate-free data, refreshes and cancels", async () => {
  const run = await setup();
  run.flush();
  expect(run.layout).toHaveBeenCalled();
  let graph = run.layout.mock.results.at(-1).value;
  expect(graph.nodes).toHaveLength(2);
  expect(graph.edges).toEqual([{ source: "a", target: "b", kind: null }]);
  const transform = document.querySelector("canvas").__zoom;
  for (const n of graph.nodes) {
    const [x, y] = transform.apply([n.x, n.y]);
    expect(x).toBeGreaterThan(0);
    expect(x).toBeLessThan(800);
    expect(y).toBeGreaterThan(0);
    expect(y).toBeLessThan(600);
  }
  expect(run.context.arc).toHaveBeenCalled();
  const stop = vi.spyOn(graph, "stop");
  component.$set({
    nodes: [
      ...graph.nodes.map((n) => ({
        id: n.id,
        title: n.title,
        type: "fact",
        degree: 1,
      })),
      { id: "c", title: "New note", type: "fact", degree: 1 },
    ],
    edges: [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
    ],
  });
  await tick();
  expect(stop).toHaveBeenCalledOnce();
  run.flush();
  graph = run.layout.mock.results.at(-1).value;
  expect(graph.nodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
  expect(
    graph.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)),
  ).toBe(true);
  run.resize();
  run.flush();
  expect(run.layout).toHaveBeenCalledTimes(2);
  component.$destroy();
  component = undefined;
  expect(run.disconnect).toHaveBeenCalledOnce();
  expect(frames.size).toBe(0);
});

it("preserves hit testing, cluster/search masks, touched labels and zoom", async () => {
  const run = await setup();
  run.flush();
  const graph = run.layout.mock.results.at(-1).value;
  const transform = document.querySelector("canvas").__zoom;
  const canvas = document.querySelector("canvas");
  const [clientX, clientY] = transform.apply([
    graph.nodes[0].x,
    graph.nodes[0].y,
  ]);
  canvas.dispatchEvent(
    new MouseEvent("mousedown", { clientX, clientY, button: 0 }),
  );
  window.dispatchEvent(
    new MouseEvent("mouseup", { clientX, clientY, button: 0 }),
  );
  expect(run.onNodeClick).toHaveBeenCalledWith(
    expect.objectContaining({ id: "a" }),
  );
  component.$set({ searchTerm: "Alpha", touchedIds: new Set(["a"]) });
  await tick();
  run.flush();
  expect(run.context.fillText).toHaveBeenCalledWith(
    "Alpha",
    expect.any(Number),
    expect.any(Number),
  );
  run.context.arc.mockClear();
  component.$set({ activeClusters: new Set() });
  await tick();
  run.flush();
  expect(run.context.arc).not.toHaveBeenCalled();
  component.$set({ activeClusters: new Set(["fact"]), searchTerm: "" });
  await tick();
  run.flush();
  const zoomCalls = run.onZoom.mock.calls.length;
  canvas.dispatchEvent(
    new WheelEvent("wheel", {
      deltaY: -80,
      clientX: 400,
      clientY: 300,
      bubbles: true,
    }),
  );
  run.flush();
  expect(run.onZoom.mock.calls.length).toBeGreaterThan(zoomCalls);
});
