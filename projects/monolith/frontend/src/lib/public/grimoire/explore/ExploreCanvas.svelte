<script>
  // Force-directed EXPLORE canvas: ported from the interaction/render spec at
  // the grimoire reskin mockup (the EXPLORE view's
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
    // Reports the settled node positions after each layout as a Map(id ->
    // {x, y}) in sim-space, so a parent re-render (the World re-center) can
    // seed shared nodes back into place via `initialPositions`.
    onpositions = null,
    // Optional seed positions for nodes that have no position yet (id ->
    // {x, y} in sim-space). Used by the World page's re-center so a node the
    // old and new ego share stays put across the fetch instead of hash-jumping
    // to a fresh spot; a node present here but not in the current layout keeps
    // its passed-in position, anything absent still falls back to the
    // deterministic hash placement below.
    initialPositions = null,
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
  // Camera auto-fit (ported from MiniConstellation): after each settle the
  // view is fitted to the settled bounding box. The first fit (and resizes)
  // snaps; later fits (a scope/lens/ego change re-centering the graph) ease
  // from the current view to the new fit so on-screen nodes never jump. A user
  // pan/zoom sets `userMoved`, which suppresses auto-fit until the next
  // content change resets it (a data change is a fresh view, a manual nudge is
  // not undone under the user's fingers).
  const CAMERA_MS = 520;
  let hasFit = false;
  let userMoved = false;
  let viewAnim = null;
  // Reactive (unlike the rest of the sim state above): bound to a template
  // `class:dragging`, so it needs Svelte to notice it changed.
  let dragging = $state(false);

  const REDUCED_MOTION =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Resolved theme colors, re-read from computed style when the class list on
  // the .grimoire root changes (see the MutationObserver in onMount) or the
  // node set changes (a new entity_type may need its own token read). Read
  // off `canvasEl` itself: it lives inside .grimoire, so getComputedStyle
  // resolves the same cascaded/scoped custom properties the CSS would use,
  // without hunting for the .grimoire ancestor at read time.
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

  // Deterministic id-hash placement: gives every node a stable initial
  // position (independent of its index in the incoming array), so the
  // settled layout looks the same across visits instead of reshuffling
  // whenever the fetch order changes.
  function hashOf(id) {
    const s = String(id);
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }

  // ── layout rebuild: runs whenever the parent hands us a new nodes/edges
  // pair (a scope/lens change, an ego merge, ...). Existing node ids keep
  // their current x/y/vx/vy (continuity across a merge); new ids get an
  // initial hash-based placement and are tagged `isNew` so startSim() knows
  // to fade them in once settled.
  $effect(() => {
    const nextNodes = nodes;
    const nextEdges = edges;
    const prevById = byId;
    const nextById = new Map();
    const nextSim = nextNodes.map((n) => {
      const prev = prevById.get(n.id);
      const seed = prev ? null : initialPositions?.get?.(n.id);
      const h = hashOf(n.id);
      const angle = ((h % 3600) / 3600) * Math.PI * 2;
      const radius = 108 + ((h >>> 12) % 4) * 34;
      const node = {
        ...n,
        // Position priority: an existing sim node keeps its live position
        // (continuity across a merge); otherwise a caller-seeded position
        // (World re-center reusing the old layout); otherwise the
        // deterministic hash placement.
        x: prev?.x ?? seed?.x ?? Math.cos(angle) * radius,
        y: prev?.y ?? seed?.y ?? Math.sin(angle) * radius,
        vx: prev?.vx ?? 0,
        vy: prev?.vy ?? 0,
        deg: 0,
        // A seeded node is not "new": it carries a known position from the
        // previous view, so it should not fade in as if it just appeared.
        isNew: !prev && !seed,
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

  // Fade-in factor for a node that just settled: 0 right when it's born, 1
  // once 280ms have passed. Nodes without a `bornAt` (already settled, or
  // reduced-motion) are always fully opaque. Clamped so `performance.now()`
  // jitter that puts `bornAt` slightly in the future never goes negative.
  function fadeOf(n, now) {
    if (!n.bornAt) return 1;
    return Math.max(0, Math.min(1, (now - n.bornAt) / 280));
  }

  function draw() {
    if (!ctx) return;
    const now = performance.now();
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
    // Alpha is further scaled by the fade of whichever endpoint is still
    // fading in (min of the two), so an edge never appears ahead of either
    // node it connects.
    visEdges.forEach((e) => {
      const a0 = byId.get(e.from);
      const b0 = byId.get(e.to);
      const fade = Math.min(fadeOf(a0, now), fadeOf(b0, now));
      if (fade <= 0) return;
      const a = toScreen(a0);
      const b = toScreen(b0);
      const incident = focusId && (e.from === focusId || e.to === focusId);
      ctx.strokeStyle = incident ? colors.accent : colors.line;
      ctx.globalAlpha = (focusId ? (incident ? 0.9 : 0.35) : 0.7) * fade;
      ctx.lineWidth = incident ? 1.8 : 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;

    // Nodes. A fading-in node also scales its radius from 0.6x to 1x with an
    // ease-out cubic, so it appears to grow into place rather than travel
    // (position is never animated, only opacity and scale).
    sim.forEach((n) => {
      const fade = fadeOf(n, now);
      if (fade <= 0) return;
      const p = toScreen(n);
      const rFull = radiusOf(n);
      const scale = 0.6 + 0.4 * (1 - Math.pow(1 - fade, 3));
      const r = rFull * scale;
      const dim = focusId && n.id !== focusId && !focusAdj?.has(n.id);
      ctx.globalAlpha = (dim ? 0.32 : 1) * fade;
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
        ctx.globalAlpha = fade;
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 5, 0, Math.PI * 2);
        ctx.stroke();
      } else if (n.id === hoverId) {
        ctx.globalAlpha = 0.85 * fade;
        ctx.strokeStyle = colors.ink;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2);
        ctx.stroke();
      }
      // Guest ring: a node pulled in from outside the current scope/lens by
      // following a cross-lens edge (dashed accent ring).
      if (guestIds.has(n.id) && n.id !== focusId) {
        ctx.globalAlpha = 0.9 * fade;
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
    // Fade with the node so a label never appears before its node does.
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    sim.forEach((n) => {
      const fade = fadeOf(n, now);
      if (fade <= 0) return;
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
      ctx.globalAlpha = fade;
      ctx.strokeText(n.name, p.x, ty);
      ctx.fillStyle = dim ? colors.faint : isFocus ? colors.ink : colors.dim;
      ctx.fillText(n.name, p.x, ty);
    });
    ctx.globalAlpha = 1;
  }

  function requestDraw() {
    // If the fade loop is already driving frames, it will draw on its next
    // tick; otherwise (settled / reduced-motion) draw once, right now.
    if (!rafId) draw();
  }

  function reportPositions() {
    if (!onpositions) return;
    const map = new Map();
    for (const n of sim) map.set(n.id, { x: n.x, y: n.y });
    onpositions(map);
  }

  let fadeUntil = 0;

  // Run the physics forward a fixed number of steps synchronously, off
  // screen, so the layout is already at rest before anything is drawn: the
  // user never watches the simulation converge, only new nodes fade in
  // afterward (see startSim/fadeFrame below).
  function settleNow() {
    energy = 1;
    for (let i = 0; i < 220; i++) tick();
    energy = 0;
  }

  // Fit the view to the settled bounding box with padding, clamped so a lone
  // node or a dense cluster both stay legible. `snap` (resize, reduced motion,
  // first fit) applies instantly; otherwise the camera eases from the current
  // view. Suppressed once the user has panned/zoomed so a redraw never yanks
  // the view out from under them.
  function fitToContent(snap = false) {
    if (!sim.length || !width || !height) return;
    if (userMoved && !snap) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    sim.forEach((n) => {
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x);
      maxY = Math.max(maxY, n.y);
    });
    const pad = 60;
    const w = Math.max(maxX - minX, 1);
    const h = Math.max(maxY - minY, 1);
    const availW = Math.max(width - pad * 2, 1);
    const availH = Math.max(height - pad * 2, 1);
    const k = Math.max(0.4, Math.min(1.6, Math.min(availW / w, availH / h)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const target = { k, x: width / 2 - cx * k, y: height / 2 - cy * k };
    if (snap || !hasFit || REDUCED_MOTION) {
      view = target;
      viewAnim = null;
      hasFit = true;
      return;
    }
    // Skip an imperceptible refit so a same-shape rebuild does not start a
    // pointless camera glide.
    if (
      Math.abs(target.k - view.k) < 0.01 &&
      Math.abs(target.x - view.x) < 1 &&
      Math.abs(target.y - view.y) < 1
    ) {
      return;
    }
    viewAnim = { from: { ...view }, to: target, start: performance.now() };
    fadeUntil = Math.max(fadeUntil, viewAnim.start + CAMERA_MS);
    if (!rafId) rafId = requestAnimationFrame(fadeFrame);
  }

  function startSim() {
    settleNow();
    // Positions are final after settleNow (the fade loop only animates
    // opacity/scale, never moves nodes): report them so a re-center can seed
    // shared nodes back into place.
    reportPositions();
    // A content change is a fresh view: re-enable auto-fit so the new graph
    // centers even if the user had panned the previous one.
    userMoved = false;
    fitToContent();
    if (REDUCED_MOTION) {
      // No fades: everything is visible at full alpha from the first draw.
      sim.forEach((n) => delete n.bornAt);
      draw();
      return;
    }
    // Stamp newly-added nodes with a staggered birth time, then run a short
    // rAF loop that only drives the opacity/scale fade (draw() reads
    // `bornAt` to compute each node's fade factor). The layout itself is
    // already settled, so this loop never moves anything, it only redraws
    // while nodes are still fading in.
    const now = performance.now();
    let i = 0;
    sim.forEach((n) => {
      if (n.isNew) {
        n.bornAt = now + Math.min(i * 14, 350);
        i++;
      }
    });
    if (i > 0) {
      fadeUntil = now + 320 + Math.min(i * 14, 350);
      if (!rafId) rafId = requestAnimationFrame(fadeFrame);
    } else if (viewAnim) {
      // No new nodes but the camera is easing to a new fit (e.g. an ego
      // re-center where every node was shared/seeded): drive frames for the
      // glide even though nothing is fading in.
      if (!rafId) rafId = requestAnimationFrame(fadeFrame);
    } else {
      // Rebuild with no new nodes and no camera glide (an edges-only change):
      // nothing to animate, so just draw the settled layout once.
      draw();
    }
  }

  function fadeFrame() {
    if (viewAnim) {
      const p = Math.min(1, (performance.now() - viewAnim.start) / CAMERA_MS);
      const e = 1 - Math.pow(1 - p, 3);
      view = {
        k: viewAnim.from.k + (viewAnim.to.k - viewAnim.from.k) * e,
        x: viewAnim.from.x + (viewAnim.to.x - viewAnim.from.x) * e,
        y: viewAnim.from.y + (viewAnim.to.y - viewAnim.from.y) * e,
      };
      if (p >= 1) viewAnim = null;
    }
    draw();
    if (performance.now() < fadeUntil || viewAnim) {
      rafId = requestAnimationFrame(fadeFrame);
    } else {
      rafId = null;
      sim.forEach((n) => delete n.bornAt);
      draw();
    }
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
    // A container geometry change re-fits instantly (easing here would read as
    // lag). `snap` overrides the userMoved guard: a genuine resize should
    // always bring the content back into view.
    fitToContent(true);
    requestDraw();
  }

  onMount(() => {
    ctx = canvasEl.getContext("2d");
    resize();
    refreshColors();
    startSim();

    const ro = new ResizeObserver(() => resize());
    ro.observe(stageEl);

    // Re-read colors if the class list on the nearest .grimoire ancestor ever
    // changes, so the canvas repaints with the current palette immediately
    // instead of on the next data change. There is no dark mode today; this
    // is a harmless no-op that keeps color init robust to a future class
    // change on that root.
    const root = canvasEl.closest(".grimoire");
    let mo = null;
    if (root) {
      mo = new MutationObserver(() => refreshColors());
      mo.observe(root, { attributes: true, attributeFilter: ["class"] });
    }

    // Unified pointer handling (mouse, touch, pen) via Pointer Events: one
    // active pointer pans (or, if it didn't move, taps-to-select on
    // release); two active pointers pinch-zoom, anchored at their midpoint.
    // Wheel-zoom (below) stays separate, it's desktop-only and orthogonal
    // to pointer tracking.
    let pointers = new Map(); // pointerId -> last-seen {x, y} in client coords
    let panStart = null; // {x, y, vx, vy} baseline for the single-pointer case
    let pinchStart = null; // {dist, k, viewX, viewY, midX, midY} for the two-pointer case
    let moved = false;

    function currentRect() {
      return canvasEl.getBoundingClientRect();
    }

    // Pinch geometry from the current pointer positions: distance between
    // the two pointers (for scale) and their midpoint in canvas-local coords
    // (the zoom anchor).
    function pinchGeometry() {
      const pts = [...pointers.values()];
      const dx = pts[0].x - pts[1].x;
      const dy = pts[0].y - pts[1].y;
      const dist = Math.hypot(dx, dy) || 1;
      const rect = currentRect();
      return {
        dist,
        midX: (pts[0].x + pts[1].x) / 2 - rect.left,
        midY: (pts[0].y + pts[1].y) / 2 - rect.top,
      };
    }

    function onPointerDown(e) {
      canvasEl.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 1) {
        panStart = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
        moved = false;
        dragging = true;
      } else if (pointers.size === 2) {
        panStart = null;
        const { dist, midX, midY } = pinchGeometry();
        pinchStart = {
          dist,
          k: view.k,
          viewX: view.x,
          viewY: view.y,
          midX,
          midY,
        };
        moved = true; // a pinch is never a tap, even if it started as one
      }
    }

    function onPointerMove(e) {
      if (!pointers.has(e.pointerId)) {
        // No gesture owns this pointer: hover-only (desktop mouse moving
        // without a button held).
        const rect = currentRect();
        const h = hitTest(e.clientX - rect.left, e.clientY - rect.top);
        if (h !== hoverId) {
          hoverId = h;
          requestDraw();
        }
        return;
      }
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

      if (pointers.size >= 2 && pinchStart) {
        const { dist, midX, midY } = pinchGeometry();
        const k2 = Math.max(
          0.4,
          Math.min(3, pinchStart.k * (dist / pinchStart.dist)),
        );
        const wx = (pinchStart.midX - pinchStart.viewX) / pinchStart.k;
        const wy = (pinchStart.midY - pinchStart.viewY) / pinchStart.k;
        view.k = k2;
        view.x = midX - wx * k2;
        view.y = midY - wy * k2;
        // A manual pinch owns the view: suppress auto-fit and cancel any
        // in-flight camera glide so the gesture is not fought by an ease.
        userMoved = true;
        viewAnim = null;
        requestDraw();
        return;
      }

      if (panStart) {
        const dx = e.clientX - panStart.x;
        const dy = e.clientY - panStart.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
        view.x = panStart.vx + dx;
        view.y = panStart.vy + dy;
        userMoved = true;
        viewAnim = null;
        requestDraw();
      }
    }

    function onPointerUp(e) {
      const wasTap = pointers.size === 1 && panStart && !moved;
      const rect = currentRect();
      const tapPos = wasTap
        ? { x: e.clientX - rect.left, y: e.clientY - rect.top }
        : null;

      pointers.delete(e.pointerId);
      if (canvasEl.hasPointerCapture?.(e.pointerId)) {
        canvasEl.releasePointerCapture(e.pointerId);
      }

      if (pointers.size >= 2) {
        // Still pinching with whichever two pointers remain: rebase so the
        // gesture continues smoothly instead of jumping.
        const { dist, midX, midY } = pinchGeometry();
        pinchStart = {
          dist,
          k: view.k,
          viewX: view.x,
          viewY: view.y,
          midX,
          midY,
        };
        panStart = null;
      } else if (pointers.size === 1) {
        // Dropped from a pinch to a single finger: rebase to a pan from
        // here, not a tap (the gesture already moved the view).
        pinchStart = null;
        const pos = pointers.values().next().value;
        panStart = { x: pos.x, y: pos.y, vx: view.x, vy: view.y };
        moved = true;
        dragging = true;
      } else {
        pinchStart = null;
        panStart = null;
        dragging = false;
      }

      if (wasTap) onselect?.(hitTest(tapPos.x, tapPos.y));
    }

    function onPointerCancel(e) {
      pointers.delete(e.pointerId);
      if (pointers.size === 0) {
        panStart = null;
        pinchStart = null;
        dragging = false;
      }
    }

    function onWheel(e) {
      e.preventDefault();
      const rect = canvasEl.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const wx = (mx - view.x) / view.k;
      const wy = (my - view.y) / view.k;
      const k2 = Math.max(
        0.4,
        Math.min(3, view.k * (e.deltaY < 0 ? 1.12 : 0.89)),
      );
      view.k = k2;
      view.x = mx - wx * k2;
      view.y = my - wy * k2;
      userMoved = true;
      viewAnim = null;
      requestDraw();
    }

    canvasEl.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerCancel);
    canvasEl.addEventListener("wheel", onWheel, { passive: false });

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      ro.disconnect();
      mo?.disconnect();
      canvasEl.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerCancel);
      canvasEl.removeEventListener("wheel", onWheel);
    };
  });
</script>

<div class="canvas-stage" bind:this={stageEl}>
  <canvas
    bind:this={canvasEl}
    class:dragging
    role="application"
    aria-label={`Entity relationship graph, ${nodes.length} entities, ${edges.length} relationships shown. Tap or click a node to open its detail, drag to pan, scroll or pinch to zoom.`}
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
    /* Own all pan/pinch/tap gestures ourselves; without this the browser
       scrolls or zooms the page instead of the canvas on touch devices. */
    touch-action: none;
  }

  canvas.dragging {
    cursor: grabbing;
  }
</style>
