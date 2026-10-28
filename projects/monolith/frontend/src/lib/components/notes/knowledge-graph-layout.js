import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCollide,
  forceX,
  forceY,
} from "d3-force";

export function radiusFor(degree, { baseRadius, hubBoost }) {
  return baseRadius + hubBoost * Math.log2(1 + (degree || 0));
}

// Include interior changes and edge rewiring, while ignoring legacy API x/y.
export function graphFingerprint(nodes, edges) {
  return JSON.stringify([
    nodes.map(({ id, title, type, degree }) => [id, title, type, degree]),
    edges.map(({ source, target, kind, edge_type }) => [
      source,
      target,
      kind ?? edge_type,
    ]),
  ]);
}

/**
 * Local mutable nodes, string-ID render edges, and separate D3 link objects.
 * D3's deterministic phyllotaxis seeds every node, including coordinate-free
 * newcomers and disconnected components. Cached server coordinates are ignored.
 * The simulation is stopped immediately: only our cancellable frame batches
 * advance it, at most 120 ticks and at most 8ms per batch (plus one tick).
 */
export function layoutGraph(
  nodes,
  edges,
  {
    width,
    height,
    config,
    colorFor,
    onTick,
    requestFrame = requestAnimationFrame,
    cancelFrame = cancelAnimationFrame,
    now = () => performance.now(),
  },
) {
  const seen = new Set();
  const localNodes = nodes
    .filter((n) => {
      if (typeof n.id !== "string" || seen.has(n.id)) return false;
      seen.add(n.id);
      return true;
    })
    .map((n) => ({
      id: n.id,
      title: n.title ?? "",
      cluster: n.type,
      degree: n.degree ?? 0,
      r: radiusFor(n.degree ?? 0, config),
      color: colorFor(n.type),
    }));
  const localEdges = edges
    .filter((e) => seen.has(e.source) && seen.has(e.target))
    .map((e) => ({
      source: e.source,
      target: e.target,
      kind: e.kind ?? e.edge_type ?? null,
    }));
  const simulation = forceSimulation(localNodes)
    .stop()
    .force(
      "link",
      forceLink(localEdges.map((e) => ({ ...e })))
        .id((n) => n.id)
        .distance(35),
    )
    .force("charge", forceManyBody().strength(-35).distanceMax(600))
    .force(
      "collide",
      forceCollide((n) => n.r + 3),
    )
    .force("x", forceX(width / 2).strength(0.035))
    .force("y", forceY(height / 2).strength(0.035));
  let frame = 0;
  let ticks = 0;
  let stopped = false;
  function advance() {
    frame = 0;
    if (stopped) return;
    const started = now();
    do {
      simulation.tick();
      ticks++;
    } while (ticks < 120 && now() - started < 8);
    onTick(ticks === 120);
    if (!stopped && ticks < 120) frame = requestFrame(advance);
  }
  if (localNodes.length) frame = requestFrame(advance);
  return {
    nodes: localNodes,
    edges: localEdges,
    stop() {
      stopped = true;
      if (frame) cancelFrame(frame);
      frame = 0;
      simulation.stop();
    },
  };
}
