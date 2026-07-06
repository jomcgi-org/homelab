<script>
  // Force-directed EXPLORE canvas: ported from the interaction/render spec at
  // docs/plans/assets/2026-07-05-grimoire-reskin-mockup.html (the EXPLORE view's
  // exTick/exDraw/exHit/pan/zoom code), swapped from globals to a Svelte 5
  // component. Purely presentational: the parent owns data-fetching, scope/lens
  // state, and the current selection; this component only simulates layout,
  // draws, and reports hit-tests back via `onselect`.
  //
  // Node/edge positions are kept in a PLAIN (non-reactive) array, not Svelte
  // `$state`: the physics loop mutates x/y/vx/vy up to 60 times a second, and
  // wrapping that in Svelte's reactivity proxy would mean a proxy trap on every
  // field write of every node every frame. The canvas is redrawn imperatively
  // from requestAnimationFrame instead of from Svelte's render cycle.
  import { onMount } from "svelte";

  let {
    nodes = [],
    edges = [],
    focusId = null,
    guestIds = new Set(),
    onselect = null,
  } = $props();

  let stageEl;
  let canvasEl;
  /** @type {CanvasRenderingContext2D} */
  let ctx;
  let width = 0;
  let height = 0;
  let dpr = 1;

  // Plain simulation state (see note above): `sim` holds one draw/physics
  // object per visible node, `byId` indexes it for O(1) edge lookups, and
  // `visEdges` is `edges` filtered down to pairs where both endpoints have a
  // simulation node (an edge whose other end never loaded is silently
  // dropped rather than drawn dangling).
  let sim = [];
  let byId = new Map();
  let visEdges = [];

  let view = { x: 0, y: 0, k: 1 };
  let energy = 1;
  let rafId = null;
  let hoverId = null;
  // Reactive (unlike the rest of the sim state above): bound to a template
  // `class:dragging`, so it needs Svelte to notice it changed.
  let dragging = $state(false);

  const REDUCED_MOTION =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Resolved theme colors, re-read from computed style whenever the .grimoire
  // root's dark class flips (see the MutationObserver in onMount) or the node
  // set changes (a new entity_type may need its own token read). Read off
  // `canvasEl` itself: it lives inside .grimoire, so getComputedStyle resolves
  // the same cascaded/scoped custom properties the CSS would use, light or
  // dark, without hunting for the .grimoire ancestor at read time.
  let colors = { line: "", ink: "", dim: "", faint: "", accent: "", paper: "" };
  let typeColors = {};

  function readVar(name, fallback) {
    if (!canvasEl) return fallback;
    const v = getComputedStyle(canvasEl).getPropertyValue(name).trim();
    return v || fallback;
  }

  function refreshColors() {
    if (!canvasEl) return;
    colors = {
      line: readVar("--grim-line", "#999"),
      ink: readVar("--grim-ink", "#111"),
      dim: readVar("--grim-text-dim", "#666"),
      faint: readVar("--grim-text-faint", "#999"),
      accent: readVar("--grim-accent", "#33507a"),
      paper: readVar("--grim-paper", "#fff"),
    };
    const nextTypeColors = {};
    for (const n of sim) {
      if (!(n.entity_type in nextTypeColors)) {
        nextTypeColors[n.entity_type] = readVar(
          `--grim-type-${n.entity_type}`,
          colors.faint,
        );
      }
    }
    typeColors = nextTypeColors;
    requestDraw();
  }

  function nodeColor(n) {
    return typeColors[n.entity_type] ?? colors.faint;
  }

  // ── layout rebuild: runs whenever the parent hands us a new nodes/edges
  // pair (a scope/lens change, an ego merge, ...). Existing node ids keep
  // their current x/y/vx/vy (continuity across a merge); new ids get an
  // initial ring placement identical to the mockup's bootstrap layout.
  $effect(() => {
    const nextNodes = nodes;
    const nextEdges = edges;
    const prevById = byId;
    const nextById = new Map();
    const nextSim = nextNodes.map((n, i) => {
      const prev = prevById.get(n.id);
      const angle = (i / Math.max(nextNodes.length, 1)) * Math.PI * 2;
      const radius = 108 + (i % 4) * 34;
      const node = {
        ...n,
        x: prev?.x ?? Math.cos(angle) * radius,
        y: prev?.y ?? Math.sin(angle) * radius,
        vx: prev?.vx ?? 0,
        vy: prev?.vy ?? 0,
        deg: 0,
      };
      nextById.set(n.id, node);
      return node;
    });
    const nextVisEdges = nextEdges.filter(
      (e) => nextById.has(e.from) && nextById.has(e.to),
    );
    nextVisEdges.forEach((e) => {
      nextById.get(e.from).deg++;
      nextById.get(e.to).deg++;
    });
    sim = nextSim;
    byId = nextById;
    visEdges = nextVisEdges;
    refreshColors();
    startSim();
  });

  // Redraw (no re-layout) when only the selection or guest set changes.
  $effect(() => {
    void focusId;
    void guestIds;
    requestDraw();
  });

  function toScreen(n) {
    return { x: n.x * view.k + view.x, y: n.y * view.k + view.y };
  }

  function radiusOf(n) {
    return (6 + Math.min(n.deg, 7) * 1.6) * Math.sqrt(view.k);
  }

  // One physics step: pairwise repulsion, edge springs toward a rest length,
  // a weak centering pull, and velocity damping. Same constants as the
  // mockup (tuned there for a few dozen nodes, which is this corpus's usual
  // scope/lens subgraph size).
  function tick() {
    for (let i = 0; i < sim.length; i++) {
      const a = sim[i];
      for (let j = i + 1; j < sim.length; j++) {
        const b = sim[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy || 0.01;
        const f = 6200 / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }
    visEdges.forEach((e) => {
      const a = byId.get(e.from);
      const b = byId.get(e.to);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - 104) * 0.03;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    });
    sim.forEach((n) => {
      n.vx += -n.x * 0.0018;
      n.vy += -n.y * 0.0018;
      n.vx *= 0.86;
      n.vy *= 0.86;
      n.x += n.vx * energy;
      n.y += n.vy * energy;
    });
  }

  function draw() {
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const focusAdj = focusId
      ? new Set(
          visEdges
            .filter((e) => e.from === focusId || e.to === focusId)
            .map((e) => (e.from === focusId ? e.to : e.from)),
        )
      : null;

    // Edges: quiet line color, incident-to-focus edges promoted to accent.
    visEdges.forEach((e) => {
      const a = toScreen(byId.get(e.from));
      const b = toScreen(byId.get(e.to));
      const incident = focusId && (e.from === focusId || e.to === focusId);
      ctx.strokeStyle = incident ? colors.accent : colors.line;
      ctx.globalAlpha = focusId ? (incident ? 0.9 : 0.35) : 0.7;
      ctx.lineWidth = incident ? 1.8 : 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;

    // Nodes.
    sim.forEach((n) => {
      const p = toScreen(n);
      const r = radiusOf(n);
      const dim = focusId && n.id !== focusId && !focusAdj?.has(n.id);
      ctx.globalAlpha = dim ? 0.32 : 1;
      ctx.shadowColor = "rgba(20,30,50,0.25)";
      ctx.shadowBlur = dim ? 0 : 6;
      ctx.shadowOffsetY = dim ? 0 : 1;
      ctx.fillStyle = nodeColor(n);
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.shadowOffsetY = 0;
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = colors.paper;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.stroke();

      if (n.id === focusId) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 5, 0, Math.PI * 2);
        ctx.stroke();
      } else if (n.id === hoverId) {
        ctx.globalAlpha = 0.85;
        ctx.strokeStyle = colors.ink;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2);
        ctx.stroke();
      }
      // Guest ring: a node pulled in from outside the current scope/lens by
      // following a cross-lens edge (dashed accent ring).
      if (guestIds.has(n.id) && n.id !== focusId) {
        ctx.globalAlpha = 0.9;
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    });

    // Labels, always on (readability over declutter, per the mockup).
    ctx.globalAlpha = 1;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    sim.forEach((n) => {
      const p = toScreen(n);
      const r = radiusOf(n);
      const isFocus = n.id === focusId;
      const isNeighbor = focusAdj?.has(n.id);
      const dim = focusId && !isFocus && !isNeighbor;
      ctx.font =
        (isFocus ? "600 14px " : "13px ") +
        '"Iowan Old Style", Palatino, Georgia, serif';
      const ty = p.y + r + 5;
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = colors.paper;
      ctx.strokeText(n.name, p.x, ty);
      ctx.fillStyle = dim ? colors.faint : isFocus ? colors.ink : colors.dim;
      ctx.fillText(n.name, p.x, ty);
    });
  }

  function requestDraw() {
    // If the sim loop is already driving frames, it will draw on its next
    // tick; otherwise (settled / reduced-motion) draw once, right now.
    if (!rafId) draw();
  }

  function loopFrame() {
    if (energy > 0.001) {
      tick();
      energy *= 0.992;
    } else {
      energy = 0;
    }
    draw();
    rafId = energy > 0 ? requestAnimationFrame(loopFrame) : null;
  }

  function startSim() {
    if (REDUCED_MOTION) {
      // Settle instantly (no visible motion): run the physics forward a
      // fixed number of steps synchronously, then draw the resting layout
      // once and never start the animation loop.
      energy = 1;
      for (let i = 0; i < 220; i++) tick();
      energy = 0;
      draw();
      return;
    }
    energy = 1;
    if (!rafId) rafId = requestAnimationFrame(loopFrame);
  }

  function hitTest(mx, my) {
    for (let i = sim.length - 1; i >= 0; i--) {
      const n = sim[i];
      const p = toScreen(n);
      const r = radiusOf(n) + 6;
      if ((mx - p.x) ** 2 + (my - p.y) ** 2 <= r * r) return n.id;
    }
    return null;
  }

  function resize() {
    if (!canvasEl) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = canvasEl.clientWidth;
    height = canvasEl.clientHeight;
    canvasEl.width = width * dpr;
    canvasEl.height = height * dpr;
    requestDraw();
  }

  onMount(() => {
    ctx = canvasEl.getContext("2d");
    resize();
    view.x = width / 2;
    view.y = height / 2;
    refreshColors();
    startSim();

    const ro = new ResizeObserver(() => resize());
    ro.observe(stageEl);

    // Re-read colors whenever the .grimoire root's light/dark class flips
    // (ThemeToggle.svelte toggles a "dark" class on the nearest .grimoire
    // ancestor, not document.body), so the canvas repaints with the new
    // palette immediately instead of on the next data change.
    const root = canvasEl.closest(".grimoire");
    let mo = null;
    if (root) {
      mo = new MutationObserver(() => refreshColors());
      mo.observe(root, { attributes: true, attributeFilter: ["class"] });
    }

    let down = null;
    let moved = false;

    function onMouseDown(e) {
      const rect = canvasEl.getBoundingClientRect();
      down = {
        x: e.clientX,
        y: e.clientY,
        vx: view.x,
        vy: view.y,
        rectLeft: rect.left,
        rectTop: rect.top,
      };
      moved = false;
      dragging = true;
    }

    function onMouseUp(e) {
      dragging = false;
      if (down && !moved) {
        const rect = canvasEl.getBoundingClientRect();
        const id = hitTest(e.clientX - rect.left, e.clientY - rect.top);
        onselect?.(id);
      }
      down = null;
    }

    function onMouseMove(e) {
      const rect = canvasEl.getBoundingClientRect();
      if (down) {
        const dx = e.clientX - down.x;
        const dy = e.clientY - down.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
        view.x = down.vx + dx;
        view.y = down.vy + dy;
        requestDraw();
      } else {
        const h = hitTest(e.clientX - rect.left, e.clientY - rect.top);
        if (h !== hoverId) {
          hoverId = h;
          requestDraw();
        }
      }
    }

    function onWheel(e) {
      e.preventDefault();
      const rect = canvasEl.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const wx = (mx - view.x) / view.k;
      const wy = (my - view.y) / view.k;
      const k2 = Math.max(0.4, Math.min(3, view.k * (e.deltaY < 0 ? 1.12 : 0.89)));
      view.k = k2;
      view.x = mx - wx * k2;
      view.y = my - wy * k2;
      requestDraw();
    }

    canvasEl.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mouseup", onMouseUp);
    window.addEventListener("mousemove", onMouseMove);
    canvasEl.addEventListener("wheel", onWheel, { passive: false });

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      ro.disconnect();
      mo?.disconnect();
      canvasEl.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("mouseup", onMouseUp);
      window.removeEventListener("mousemove", onMouseMove);
      canvasEl.removeEventListener("wheel", onWheel);
    };
  });
</script>

<div class="canvas-stage" bind:this={stageEl}>
  <canvas
    bind:this={canvasEl}
    class:dragging
    role="application"
    aria-label={`Entity relationship graph, ${nodes.length} entities, ${edges.length} relationships shown. Click a node to open its detail, drag to pan, scroll to zoom.`}
  ></canvas>
</div>

<style>
  .canvas-stage {
    position: relative;
    width: 100%;
    height: 100%;
  }

  canvas {
    display: block;
    width: 100%;
    height: 100%;
    cursor: grab;
  }

  canvas.dragging {
    cursor: grabbing;
  }
</style>
