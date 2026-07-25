<script>
  // Knowledge graph rendered to a canvas. Layout positions (`node.x`,
  // `node.y`) are computed server-side by the gardener every reconcile
  // cycle and shipped on the wire — the client just paints. We keep the
  // quadtree hit-test, the d3-zoom pan/zoom + click-vs-drag detection,
  // viewport-aware zoom-to-node, and the greedy-collision label drawer.
  // Search box, legend, side panel, and status bar live in sibling
  // components and drive this one via props/events.

  import { onMount } from "svelte";
  import { select } from "d3-selection";
  import { zoom as d3Zoom, zoomIdentity } from "d3-zoom";
  import { quadtree as d3Quadtree } from "d3-quadtree";
  import { colorFor, CLUSTER_COLORS } from "./clusters.js";
  import { radiusFor, projectXY } from "./knowledge-graph-layout.js";

  /**
   * @typedef {{ id: string, title: string, type: string, degree: number }} Node
   * @typedef {{ source: string, target: string, kind?: string, edge_type?: string }} Edge
   */

  /** @type {{
   *   nodes?: Node[],
   *   edges?: Edge[],
   *   selectedId?: string | null,
   *   searchTerm?: string,
   *   activeClusters?: Set<string>,
   *   touchedIds?: Set<string>,
   * }} */
  let {
    nodes = [],
    edges = [],
    selectedId = null,
    searchTerm = "",
    activeClusters = new Set(),
    // The set of node ids a public-chat turn grounded on (ADR 005). When
    // supplied (the /app/notes graph view), these nodes get a distinct accent
    // halo + always-on label so "the nodes your chat touched" read at a glance.
    // Empty on the private /notes graph, where this pass is a no-op.
    touchedIds = new Set(),
    onNodeClick = () => {},
    onNodeHover = () => {},
    onZoom = () => {},
  } = $props();

  // Render-time tuning only. Layout (positions, spread, edge tension)
  // runs server-side in projects/monolith/knowledge/layout.py
  // (FA2 + hard collide post-process). The render code does not own
  // any layout parameters.
  //
  // baseRadius/hubBoost reduced 35% from 2.8/0.5 in feat/kg-layout-v2
  // to reduce visible overlap in dense atom clusters; combined with
  // API-side orphan filtering and a hard collide post-process this
  // minimizes node overlap while preserving the FA2 cluster shape.
  const CFG = {
    baseRadius: 1.82,
    hubBoost: 0.325,
    edgeOpacity: 0.16,
    edgeOpacityActive: 0.85,
    // Labels turn on only when the user has actively zoomed in past
    // any default-fit state. ``fitToBbox()`` caps initial zoom at 1.5×
    // (see line ~548), so this threshold sits clearly above that —
    // default page-load shows zero labels regardless of layout size,
    // selection still gets a label always, search still labels its
    // matches, and zooming in past 1.6× turns on the high-degree
    // visible-node labels.
    labelMinZoom: 1.6,
    labelMaxCount: 60,
  };

  // Internal mutable state. simNodes/simEdges are local copies of the
  // server-supplied data with x/y rescaled into pixel space and edges
  // kept as string-id references (we resolve to objects in render via
  // `byId.get(...)`).
  let stage; // root div
  let canvas; // <canvas>
  let ctx; // 2d context
  let dpr = 1;
  let resizeObserver;
  let quadtree;
  let transform = zoomIdentity;
  let zoomBehavior;
  // Pending requestAnimationFrame id for coalesced repaints (see
  // scheduleRender). 0 means no frame is queued.
  let rafId = 0;

  let simNodes = [];
  let simEdges = [];
  let byId = new Map();
  let neighborsOf = new Map();

  // Resolved cluster colours — concrete hex/rgb strings looked up from
  // CSS custom properties at mount time. Canvas can't consume var(...),
  // so we resolve once and hand it to the renderer.
  /** @type {Record<string, string>} */
  let resolved = {};

  // Hovered node (transient, internal). Selection is a prop.
  let hovered = $state(null);
  let panning = $state(false);
  let overNode = $state(false);
  // Last drawn label rectangles, kept around so findNode() can hit-test
  // against them. Labels are painted directly to canvas, so without
  // this lookup users can't click the label to navigate (only the
  // tiny coloured dot itself).
  let placedLabels = [];

  let mouseDownAt = null;
  let didMove = false;

  // ───────────────────────── helpers ─────────────────────────

  function resolveColors() {
    // CLUSTER_COLORS now ships concrete hex values, so this is just a
    // shallow copy plus the fallback for unknown types. Kept as a fn
    // so the call sites don't need to change.
    return { ...CLUSTER_COLORS, __other: "#FFFFFF" };
  }

  function colorForResolved(type) {
    if (resolved[type]) return resolved[type];
    return resolved.__other ?? "#888";
  }

  // Server coords arrive normalised to ~[-1, +1] (see compute_layout in
  // knowledge/layout.py — connected nodes scaled to core_fraction, then
  // a hard collide post-process spreads them out further; orphans are
  // filtered out at the API layer so they never reach this component).
  // projectXY scales them so the unit canvas fills the smaller stage
  // dimension minus a margin; fitToBbox() later refines the initial zoom.

  function rebuildGraph() {
    const w = stage?.clientWidth ?? canvas?.width ?? 1200;
    const h = stage?.clientHeight ?? canvas?.height ?? 800;
    const cx = w / 2;
    const cy = h / 2;
    const margin = 60;
    const span = Math.max(50, Math.min(w, h) / 2 - margin);

    simNodes = nodes.map((n) => {
      const [x, y] = projectXY(n, cx, cy, span);
      return {
        id: n.id,
        title: n.title,
        cluster: n.type,
        degree: n.degree ?? 0,
        r: radiusFor(n.degree ?? 0, CFG),
        color: colorForResolved(n.type),
        x,
        y,
      };
    });
    simEdges = edges.map((e) => ({
      source: e.source,
      target: e.target,
      kind: e.kind ?? e.edge_type ?? null,
    }));
    byId = new Map(simNodes.map((n) => [n.id, n]));
    neighborsOf = new Map(simNodes.map((n) => [n.id, new Set()]));
    for (const e of simEdges) {
      // Edges hold string ids; render() resolves on demand via byId.
      const s = neighborsOf.get(e.source);
      const t = neighborsOf.get(e.target);
      if (s) s.add(e.target);
      if (t) t.add(e.source);
    }
  }

  function resize() {
    if (!stage || !canvas) return;
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    canvas.width = Math.max(1, Math.floor(w * dpr));
    canvas.height = Math.max(1, Math.floor(h * dpr));
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function rebuildQuadtree() {
    quadtree = d3Quadtree()
      .x((d) => d.x)
      .y((d) => d.y)
      .addAll(simNodes);
  }

  // Builds the active filter view: which nodes survive the current
  // search term + selection, plus a flag for "any filter active".
  // Render, hit-testing, and the zoom-to-selection bbox all read this
  // so they agree about what's visible. When a filter is active,
  // matchSet IS the set of drawn nodes; everything else is skipped
  // entirely (no ghost-dim layer).
  function computeFilterState() {
    const term = (searchTerm || "").trim().toLowerCase();
    const selectedNode = selectedId ? byId.get(selectedId) : null;
    const filtering = !!term || !!selectedNode;
    const matchSet = new Set();
    if (term) {
      for (const n of simNodes) {
        if (
          activeClusters.has(n.cluster) &&
          n.title.toLowerCase().includes(term)
        ) {
          matchSet.add(n.id);
        }
      }
    } else if (selectedNode) {
      matchSet.add(selectedNode.id);
      const neigh = neighborsOf.get(selectedNode.id);
      if (neigh) for (const id of neigh) matchSet.add(id);
    }
    return { term, selectedNode, filtering, matchSet };
  }

  function findNode(mx, my) {
    // Label hit-test first: labels are big rectangles, easier targets
    // than 3px node disks. Since labels are drawn in screen coords
    // (post-transform), we test mx/my directly against their boxes.
    for (const p of placedLabels) {
      if (mx >= p.bx && mx <= p.bx + p.bw && my >= p.by && my <= p.by + p.bh) {
        return p.node;
      }
    }
    if (!quadtree) return null;
    const [x, y] = transform.invert([mx, my]);
    const r = 14 / transform.k;
    const n = quadtree.find(x, y, r);
    if (!n) return null;
    if (!activeClusters.has(n.cluster)) return null;
    // Filtered-out nodes aren't painted, so don't let the quadtree
    // claim clicks for them either — otherwise empty-looking pixels
    // would still snap-select an invisible node.
    const { filtering, matchSet } = computeFilterState();
    if (filtering && !matchSet.has(n.id)) return null;
    return n;
  }

  // ───────────────────────── render ─────────────────────────

  // Coalesce repaints to one per animation frame. d3-zoom fires many "zoom"
  // events per wheel/drag gesture, and mousemove fires a hover repaint per
  // pixel; calling render() synchronously on each made a 4.6k-node / 24.5k-edge
  // graph janky (every frame re-walks all edges twice + the O(n^2) label
  // collision pass). Requesting a single frame collapses a burst of input
  // events into one paint that reads the latest `transform`/`hovered`.
  function scheduleRender() {
    if (rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      render();
    });
  }

  function render() {
    if (!ctx || !stage) return;
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    ctx.clearRect(0, 0, w, h);

    ctx.save();
    ctx.translate(transform.x, transform.y);
    ctx.scale(transform.k, transform.k);

    const { selectedNode, filtering, matchSet } = computeFilterState();

    // Edges (faint).
    ctx.lineWidth = Math.max(0.4, 0.7 / transform.k);
    ctx.strokeStyle = `rgba(20,20,20,${CFG.edgeOpacity})`;
    ctx.beginPath();
    for (const l of simEdges) {
      const s = typeof l.source === "object" ? l.source : byId.get(l.source);
      const t = typeof l.target === "object" ? l.target : byId.get(l.target);
      if (!s || !t) continue;
      if (!activeClusters.has(s.cluster) || !activeClusters.has(t.cluster))
        continue;
      if (filtering && !(matchSet.has(s.id) && matchSet.has(t.id))) continue;
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
    }
    ctx.stroke();

    // Highlighted edges around the selected node. Same visibility
    // gates as the faint-edge pass: cluster filter + matchSet, so a
    // search term that excludes a neighbour also drops the spoke
    // pointing at that neighbour (otherwise the spoke would stop in
    // empty space where the node used to be drawn).
    if (selectedNode) {
      ctx.lineWidth = Math.max(1.2, 1.6 / transform.k);
      ctx.strokeStyle = `rgba(20,20,20,${CFG.edgeOpacityActive})`;
      ctx.beginPath();
      for (const l of simEdges) {
        const s = typeof l.source === "object" ? l.source : byId.get(l.source);
        const t = typeof l.target === "object" ? l.target : byId.get(l.target);
        if (!s || !t) continue;
        if (!activeClusters.has(s.cluster) || !activeClusters.has(t.cluster))
          continue;
        if (filtering && !(matchSet.has(s.id) && matchSet.has(t.id))) continue;
        if (s.id === selectedNode.id || t.id === selectedNode.id) {
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(t.x, t.y);
        }
      }
      ctx.stroke();
    }

    // Touched-node halo (chat overlay). Drawn BEFORE the node fill so the
    // accent disc reads as a ring around the node, with a bold ink outline for
    // the flat-ink brutalist language. Same visibility gates as the node pass
    // so a halo never floats over a hidden node.
    if (touchedIds && touchedIds.size) {
      for (const n of simNodes) {
        if (!touchedIds.has(n.id)) continue;
        if (!activeClusters.has(n.cluster)) continue;
        if (filtering && !matchSet.has(n.id)) continue;
        const hr = n.r + 5 / transform.k;
        ctx.fillStyle = "#FFDE01";
        ctx.beginPath();
        ctx.arc(n.x, n.y, hr, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#141414";
        ctx.lineWidth = Math.max(1, 1.4 / transform.k);
        ctx.beginPath();
        ctx.arc(n.x, n.y, hr, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    // Nodes. Thin stroke so the cluster colour fill dominates visually
    // — at the prototype's 3.2px radius a 1.2px stroke ate most of the
    // disk, making nodes read as black dots.
    //
    // When a filter is active, non-matching nodes are skipped entirely
    // rather than dimmed: the previous 0.08-alpha ghost layer made
    // zoomed-in selection views feel cluttered with unrelated context.
    ctx.lineWidth = Math.max(0.4, 0.8 / transform.k);
    for (const n of simNodes) {
      if (!activeClusters.has(n.cluster)) continue;
      if (filtering && !matchSet.has(n.id)) continue;
      ctx.fillStyle = n.color;
      ctx.strokeStyle = "#141414";
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }

    // Hover ring.
    if (hovered) {
      ctx.strokeStyle = "#141414";
      ctx.lineWidth = Math.max(1, 1.6 / transform.k);
      ctx.beginPath();
      ctx.arc(
        hovered.x,
        hovered.y,
        hovered.r + 3 / transform.k,
        0,
        Math.PI * 2,
      );
      ctx.stroke();
    }

    // Selected box — coral red per the prototype's aesthetic.
    if (selectedNode) {
      const s = selectedNode.r + 6 / transform.k;
      ctx.strokeStyle = "#FF6B5B";
      ctx.lineWidth = Math.max(1.5, 2 / transform.k);
      ctx.strokeRect(selectedNode.x - s, selectedNode.y - s, s * 2, s * 2);
    }

    ctx.restore();

    drawLabels(w, h, filtering, matchSet, selectedNode);
  }

  function drawLabels(w, h, filtering, matchSet, selectedNode) {
    const candidates = [];
    const seen = new Set();
    const term = (searchTerm || "").trim().toLowerCase();
    const add = (n) => {
      if (n && !seen.has(n.id) && activeClusters.has(n.cluster)) {
        seen.add(n.id);
        candidates.push(n);
      }
    };

    if (selectedNode) {
      add(selectedNode);
      const neigh = neighborsOf.get(selectedNode.id);
      if (neigh) for (const id of neigh) add(byId.get(id));
    }
    // Touched nodes (chat overlay) always carry a label so the grounded set is
    // legible without zooming in. add() already gates on activeClusters; the
    // greedy collision pass below still lets the selected node win.
    if (touchedIds && touchedIds.size) {
      for (const id of touchedIds) add(byId.get(id));
    }
    // hovered is intentionally excluded from the candidate list. If we
    // added it here, its label box would claim space in `placed` and
    // the greedy collision pass would push out other labels around it,
    // so visible labels would flicker as the mouse moved over high-
    // degree areas. Hover is a transient overlay — drawn after the
    // layout pass, on top, with no influence on which labels are shown.

    if (transform.k > CFG.labelMinZoom) {
      const [x0, y0] = transform.invert([0, 0]);
      const [x1, y1] = transform.invert([w, h]);
      const vis = [];
      for (const n of simNodes) {
        if (!activeClusters.has(n.cluster)) continue;
        if (filtering && !matchSet.has(n.id)) continue;
        if (n.x < x0 || n.x > x1 || n.y < y0 || n.y > y1) continue;
        vis.push(n);
      }
      vis.sort((a, b) => b.degree - a.degree);
      for (const n of vis) {
        add(n);
        if (candidates.length > 250) break;
      }
    } else if (filtering && term) {
      const [x0, y0] = transform.invert([0, 0]);
      const [x1, y1] = transform.invert([w, h]);
      const vis = simNodes
        .filter(
          (n) =>
            matchSet.has(n.id) &&
            n.x >= x0 &&
            n.x <= x1 &&
            n.y >= y0 &&
            n.y <= y1,
        )
        .sort((a, b) => b.degree - a.degree)
        .slice(0, 30);
      for (const n of vis) add(n);
    }

    // `placed` accumulates this frame's label rects + their owning
    // nodes; we copy it into `placedLabels` at the end so findNode()
    // can hit-test clicks against the labels.
    const placed = [];
    const fontPx = 11;
    ctx.font = `500 ${fontPx}px JetBrains Mono, ui-monospace, monospace`;
    ctx.textBaseline = "top";

    let drawn = 0;
    for (const n of candidates) {
      if (drawn >= CFG.labelMaxCount && n !== selectedNode) continue;

      const sx = n.x * transform.k + transform.x;
      const sy = n.y * transform.k + transform.y;
      const t = n.title;
      const m = ctx.measureText(t);
      const padX = 4;
      const padY = 3;
      const bw = m.width + padX * 2;
      const bh = fontPx + padY * 2;
      const bx = sx + n.r * transform.k + 5;
      const by = sy - bh / 2;

      if (bx + bw < 0 || bx > w || by + bh < 0 || by > h) continue;

      let overlap = false;
      for (const p of placed) {
        if (
          bx < p.bx + p.bw &&
          bx + bw > p.bx &&
          by < p.by + p.bh &&
          by + bh > p.by
        ) {
          overlap = true;
          break;
        }
      }
      if (overlap && n !== selectedNode) continue;
      placed.push({ bx, by, bw, bh, node: n });

      ctx.fillStyle = "#FFFFFF";
      ctx.fillRect(bx, by, bw, bh);
      ctx.lineWidth = 1;
      ctx.strokeStyle = "#141414";
      ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
      ctx.fillStyle = "#141414";
      ctx.fillText(t, bx + padX, by + padY);
      drawn++;
    }

    // Hover overlay. Painted on top of the layout pass so it doesn't
    // perturb which other labels are visible. Skipped if the loop
    // above already drew this node (selected, neighbour of selected,
    // or a high-degree visible label) — in those cases the existing
    // entry already wins click hit-tests.
    if (
      hovered &&
      hovered !== selectedNode &&
      activeClusters.has(hovered.cluster) &&
      !placed.some((p) => p.node === hovered)
    ) {
      const sx = hovered.x * transform.k + transform.x;
      const sy = hovered.y * transform.k + transform.y;
      const m = ctx.measureText(hovered.title);
      const padX = 4;
      const padY = 3;
      const bw = m.width + padX * 2;
      const bh = fontPx + padY * 2;
      const bx = sx + hovered.r * transform.k + 5;
      const by = sy - bh / 2;
      if (!(bx + bw < 0 || bx > w || by + bh < 0 || by > h)) {
        ctx.fillStyle = "#FFFFFF";
        ctx.fillRect(bx, by, bw, bh);
        ctx.lineWidth = 1;
        ctx.strokeStyle = "#141414";
        ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
        ctx.fillStyle = "#141414";
        ctx.fillText(hovered.title, bx + padX, by + padY);
        // findNode() iterates placedLabels in order; prepending gives
        // the hovered label priority for click hit-tests when it
        // visually overlaps a layout-pass label.
        placed.unshift({ bx, by, bw, bh, node: hovered });
      }
    }

    // Publish for findNode() to hit-test.
    placedLabels = placed;
  }

  // ───────────────────────── focus / filter ─────────────────────────

  function focusNode(n) {
    if (!n || !zoomBehavior || !canvas || !stage) return;
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    const m = 20;

    // Inspect the overlay rects (search/legend/panel) so the focused
    // node centres in the *visible* whitespace rather than the
    // absolute viewport. Without this the panel covers ~half the
    // canvas yet the selected node still snaps to canvas-centre,
    // which puts it under the panel.
    const stageRect = stage.getBoundingClientRect();
    // Overlays are siblings of `.stage` inside `.notes-stage`, so
    // query against the document, not the stage itself.
    const search = document.querySelector(".search")?.getBoundingClientRect();
    const legend = document.querySelector(".legend")?.getBoundingClientRect();
    const panel = document.querySelector(".panel")?.getBoundingClientRect();

    let left = m;
    if (search) left = Math.max(left, search.right - stageRect.left + m);
    if (legend) left = Math.max(left, legend.right - stageRect.left + m);
    let right = w - m;
    if (panel) right = Math.min(right, panel.left - stageRect.left - m);
    const top = m;
    const bottom = h - m;
    const acx = (left + right) / 2;
    const acy = (top + bottom) / 2;

    let x0 = n.x;
    let x1 = n.x;
    let y0 = n.y;
    let y1 = n.y;
    let count = 0;
    // Only fit the bbox to neighbours that are actually drawn — if a
    // search term excludes them, or their cluster chip is off, they
    // shouldn't pull the viewport open. Note that when only `n` itself
    // is selected (no search term), matchSet already contains it plus
    // every neighbour, so the gate is a no-op for the common case.
    const { filtering, matchSet } = computeFilterState();
    const neigh = neighborsOf.get(n.id);
    if (neigh) {
      for (const id of neigh) {
        const nb = byId.get(id);
        if (!nb || !activeClusters.has(nb.cluster)) continue;
        if (filtering && !matchSet.has(nb.id)) continue;
        if (nb.x < x0) x0 = nb.x;
        if (nb.x > x1) x1 = nb.x;
        if (nb.y < y0) y0 = nb.y;
        if (nb.y > y1) y1 = nb.y;
        count++;
      }
    }
    const bw = Math.max(60, x1 - x0);
    const bh = Math.max(60, y1 - y0);
    const pad = 40;
    const fitK = Math.min(
      Math.max(60, right - left - pad * 2) / bw,
      Math.max(60, bottom - top - pad * 2) / bh,
    );
    const k = count === 0 ? 3 : Math.max(0.6, Math.min(3.5, fitK));
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;

    select(canvas)
      .transition()
      .duration(650)
      .call(
        zoomBehavior.transform,
        zoomIdentity.translate(acx, acy).scale(k).translate(-cx, -cy),
      );
  }

  function fitToBbox() {
    if (!stage || simNodes.length === 0) return;
    // Fit only to nodes that are actually *connected* to something,
    // and use 5th–95th percentile bounds. Disconnected leaves (e.g.
    // gap stubs with no edges yet) form a ring at the equilibrium of
    // centering vs charge force and would dominate the bbox if we
    // included them — net effect is a tiny central cluster surrounded
    // by ring of "edges to nowhere", which is the opposite of "fit
    // the meaningful payload".
    const xs = [];
    const ys = [];
    for (const n of simNodes) {
      if (!activeClusters.has(n.cluster)) continue;
      const neigh = neighborsOf.get(n.id);
      if (!neigh || neigh.size === 0) continue;
      xs.push(n.x);
      ys.push(n.y);
    }
    if (xs.length === 0) return;
    xs.sort((a, b) => a - b);
    ys.sort((a, b) => a - b);
    const p = 0.05;
    const lo = Math.floor(xs.length * p);
    const hi = Math.ceil(xs.length * (1 - p)) - 1;
    const minX = xs[lo];
    const maxX = xs[hi];
    const minY = ys[lo];
    const maxY = ys[hi];
    if (!isFinite(minX)) return;
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    const bw = Math.max(1, maxX - minX);
    const bh = Math.max(1, maxY - minY);
    const padding = 60;
    const k = Math.min(
      (w - padding * 2) / bw,
      (h - padding * 2) / bh,
      // Don't zoom past 1.5× even if the layout is tiny; ratio of
      // typical zoom-in feels weird at startup.
      1.5,
    );
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const next = zoomIdentity
      .translate(w / 2, h / 2)
      .scale(k)
      .translate(-cx, -cy);
    transform = next;
    // Sync with d3.zoom's internal transform so subsequent pan/zoom
    // gestures continue from this fit instead of snapping back.
    if (zoomBehavior) {
      select(canvas).call(zoomBehavior.transform, next);
    }
  }

  // ───────────────────────── pointer handlers ─────────────────────────

  function handleMouseDown(e) {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    mouseDownAt = { mx, my, node: findNode(mx, my) };
    didMove = false;
  }

  function handleMouseMove(e) {
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    if (
      mouseDownAt &&
      Math.hypot(mx - mouseDownAt.mx, my - mouseDownAt.my) > 4
    ) {
      didMove = true;
    }

    if (e.target === canvas && !mouseDownAt) {
      const n = findNode(mx, my);
      if (n !== hovered) {
        hovered = n;
        overNode = !!n;
        onNodeHover(
          n ? { id: n.id, title: n.title } : { id: null, title: null },
        );
        scheduleRender();
      }
    }
  }

  function handleMouseUp(e) {
    if (mouseDownAt && !didMove) {
      if (mouseDownAt.node) {
        onNodeClick({ id: mouseDownAt.node.id });
      } else if (e.target === canvas) {
        onNodeClick({ id: null });
      }
    }
    mouseDownAt = null;
    panning = false;
  }

  function handleMouseLeave() {
    if (hovered) {
      hovered = null;
      overNode = false;
      onNodeHover({ id: null, title: null });
      scheduleRender();
    }
  }

  // ───────────────────────── lifecycle ─────────────────────────

  onMount(() => {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx = canvas.getContext("2d");
    resolved = resolveColors();

    // resize() before rebuildGraph() so the projection picks up the real
    // stage size — server x/y are normalised to ~[-1, +1] and we scale
    // them against the canvas dimensions.
    resize();
    rebuildGraph();
    rebuildQuadtree();
    fitToBbox();

    zoomBehavior = d3Zoom()
      .scaleExtent([0.15, 8])
      .filter((event) => {
        // Allow ALL wheel events. Trackpad pinch is delivered as
        // wheel + ctrlKey (a quirk of how browsers expose pinch
        // gestures); blocking ctrlKey here breaks pinch-to-zoom.
        // Plain mouse-wheel and trackpad-pinch both reach d3-zoom.
        if (event.type === "wheel") return true;
        if (event.type === "mousedown" || event.type === "touchstart") {
          const r = canvas.getBoundingClientRect();
          const mx =
            (event.clientX ?? event.touches?.[0]?.clientX ?? 0) - r.left;
          const my =
            (event.clientY ?? event.touches?.[0]?.clientY ?? 0) - r.top;
          return !findNode(mx, my);
        }
        return true;
      })
      .on("zoom", (e) => {
        transform = e.transform;
        onZoom(transform.k);
        if (e.sourceEvent && e.sourceEvent.type === "mousemove") {
          panning = true;
        }
        scheduleRender();
      })
      .on("end", () => {
        panning = false;
      });

    select(canvas).call(zoomBehavior);

    canvas.addEventListener("mousedown", handleMouseDown);
    canvas.addEventListener("mousemove", handleMouseMove);
    canvas.addEventListener("mouseleave", handleMouseLeave);
    window.addEventListener("mouseup", handleMouseUp);

    resizeObserver = new ResizeObserver(() => {
      resize();
      scheduleRender();
    });
    resizeObserver.observe(stage);

    // First paint with the server-supplied positions.
    render();

    // Cleanup runs on component destroy. Returning it from onMount
    // sidesteps Svelte 5's `onDestroy` which (under our build) resolves
    // to the SSR export and crashes on client (svelte/package.json
    // exports map's `default` is the server bundle).
    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      if (resizeObserver) resizeObserver.disconnect();
      if (canvas) {
        canvas.removeEventListener("mousedown", handleMouseDown);
        canvas.removeEventListener("mousemove", handleMouseMove);
        canvas.removeEventListener("mouseleave", handleMouseLeave);
      }
      if (typeof window !== "undefined") {
        window.removeEventListener("mouseup", handleMouseUp);
      }
    };
  });

  // ───────────────────────── reactive effects ─────────────────────────

  // Rebuild the graph when nodes/edges change. Tracked via referential
  // identity — the parent should pass new array refs when the data set
  // actually changes.
  //
  // BUT: the parent's `nodesWithDegree` is a $derived.by that produces
  // a new array every time the parent re-renders (any unrelated state
  // change like updating `hoverTitle` re-runs the derived). We can't
  // tell "real shape change" from "spurious new ref" just from the
  // ===, so fingerprint by length + first/last id and only rebuild
  // when that fingerprint actually changes. Otherwise hovering a node
  // would re-project all coords and the picture would micro-jitter.
  let lastDataFingerprint = "";
  $effect(() => {
    const fp =
      `${nodes.length}:${edges.length}:` +
      `${nodes[0]?.id ?? ""}:${nodes[nodes.length - 1]?.id ?? ""}`;
    if (fp === lastDataFingerprint) return;
    lastDataFingerprint = fp;
    if (!ctx) return;
    rebuildGraph();
    rebuildQuadtree();
    render();
  });

  // React to selection changes — the parent owns selectedId; we just
  // animate the viewport to it. Falsy selectedId is a no-op (the parent
  // handles closing the panel).
  $effect(() => {
    if (!ctx) return;
    if (!selectedId) {
      render();
      return;
    }
    const target = byId.get(selectedId);
    if (target) focusNode(target);
    render();
  });

  // Cluster toggle is a render-time mask, not a layout reflow: server
  // positions are computed for every node regardless of cluster (cluster
  // colours just for paint), and render() gates every node and edge on
  // activeClusters. Toggling a chip therefore just shows/hides without
  // perturbing positions — the layout stays exactly where the user left
  // it, which is the whole reason this graph feels stable.
  $effect(() => {
    activeClusters; // dependency
    if (!ctx) return;
    render();
  });

  // Search-term changes only need a re-render; render() reads the prop
  // directly to compute the dim mask.
  $effect(() => {
    searchTerm; // dependency
    if (!ctx) return;
    render();
  });

  // Touched-set changes (a new chat turn grounds on a new set of public nodes)
  // are a render-time overlay, same as the cluster mask: no layout reflow, just
  // repaint the halos + labels. Reading `.size` registers the Set as a
  // dependency without deep-tracking its members.
  $effect(() => {
    touchedIds?.size; // dependency
    if (!ctx) return;
    render();
  });
</script>

<div class="stage" bind:this={stage}>
  <canvas bind:this={canvas} class:panning class:over-node={overNode}></canvas>
</div>

<style>
  .stage {
    position: relative;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #f1ebdc; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .stage::before {
    content: "";
    position: absolute;
    inset: 0;
    background-image: radial-gradient(rgba(0, 0, 0, 0.07) 1px, transparent 1px);
    background-size: 24px 24px;
    pointer-events: none;
  }
  canvas {
    display: block;
    cursor: grab;
  }
  canvas.panning {
    cursor: grabbing;
  }
  canvas.over-node {
    cursor: pointer;
  }
</style>
