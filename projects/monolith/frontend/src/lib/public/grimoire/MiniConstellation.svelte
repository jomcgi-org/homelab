<script>
  // Small, non-interactive, pre-settled canvas graph. Same simulation and
  // draw mechanics as explore/ExploreCanvas.svelte (deterministic id-hash
  // placement, synchronous settle before the first paint, fade+scale reveal
  // only, never position), tuned for a ~300px stage and shared by the
  // entities codex and the chat constellation panel.
  //
  // Unlike ExploreCanvas this component owns no pan/zoom/tap interaction. It
  // fits its content after settling; when a growing graph (the chat panel)
  // needs more room, the camera EASES to the new fit instead of hard
  // cutting, so nodes never move relative to each other on screen (the one
  // sanctioned position change is the whole frame gliding, the landing
  // page's pull-back gesture). Callers that need an accessible equivalent
  // (pill list, node names) provide it themselves; this canvas is
  // `aria-hidden`.
  import { onMount } from "svelte";

  let {
    nodes = [],
    edges = [],
    focusId = null,
    revealedIds = null, // Set of ids to show, or null = show all
  } = $props();

  let stageEl;
  let canvasEl;
  /** @type {CanvasRenderingContext2D} */
  let ctx;
  let width = 0;
  let height = 0;
  let dpr = 1;

  // Plain simulation state, not Svelte $state: see ExploreCanvas.svelte for
  // why (per-frame field writes on a reactive proxy would be pure overhead).
  let sim = [];
  let byId = new Map();
  let visEdges = [];

  let view = { x: 0, y: 0, k: 1 };
  let rafId = null;
  // Camera state: the first fit (and any container resize) snaps; later
  // fits ease from `viewAnim.from` to `viewAnim.to` inside fadeFrame.
  let hasFit = false;
  let viewAnim = null;
  const CAMERA_MS = 300;

  const REDUCED_MOTION =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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

  function hashOf(id) {
    const s = String(id);
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }

  function isRevealed(id) {
    return revealedIds == null || revealedIds.has(id);
  }

  // Layout rebuild: runs whenever nodes/edges change. Existing ids keep their
  // x/y/vx/vy (so a relationship-pill re-target that pulls in a new focus
  // does not scramble the shared neighbors' positions); new ids get a
  // deterministic hash placement.
  $effect(() => {
    const nextNodes = nodes;
    const nextEdges = edges;
    const prevById = byId;
    const nextById = new Map();
    const nextSim = nextNodes.map((n) => {
      const prev = prevById.get(n.id);
      const h = hashOf(n.id);
      const angle = ((h % 3600) / 3600) * Math.PI * 2;
      const radius = 64 + ((h >>> 12) % 4) * 20;
      const node = {
        ...n,
        x: prev?.x ?? Math.cos(angle) * radius,
        y: prev?.y ?? Math.sin(angle) * radius,
        vx: prev?.vx ?? 0,
        vy: prev?.vy ?? 0,
        deg: 0,
        isNew: !prev,
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
    settleAndFit();
    startReveal();
  });

  // Redraw (no re-layout, no re-fit) when only the focus or reveal set
  // changes -- a reveal growing must not perturb the already-fitted view.
  $effect(() => {
    void focusId;
    void revealedIds;
    startReveal();
  });

  function toScreen(n) {
    return { x: n.x * view.k + view.x, y: n.y * view.k + view.y };
  }

  function radiusOf(n) {
    return (5 + Math.min(n.deg, 6) * 1.3) * Math.sqrt(view.k);
  }

  // Same force model as ExploreCanvas.tick(), with a shorter spring rest
  // length and weaker repulsion sized for a ~300px stage instead of the full
  // EXPLORE canvas.
  function tick() {
    for (let i = 0; i < sim.length; i++) {
      const a = sim[i];
      for (let j = i + 1; j < sim.length; j++) {
        const b = sim[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy || 0.01;
        const f = 2400 / d2;
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
      const f = (d - 64) * 0.03;
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
      n.x += n.vx;
      n.y += n.vy;
    });
  }

  // Run the physics forward off-screen, then fit the view to the settled
  // bounding box with 28px padding, clamped so a single node or a huge
  // cluster both stay legible. The first fit (and `snap` callers: resize,
  // reduced motion) applies instantly; later fits ease the camera so nodes
  // already on screen never jump when a new arrival grows the cluster.
  function settleAndFit(snap = false) {
    for (let i = 0; i < 220; i++) tick();
    if (!sim.length) return;
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
    const pad = 28;
    const w = Math.max(maxX - minX, 1);
    const h = Math.max(maxY - minY, 1);
    const availW = Math.max(width - pad * 2, 1);
    const availH = Math.max(height - pad * 2, 1);
    const k = Math.max(0.5, Math.min(1.4, Math.min(availW / w, availH / h)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const target = { k, x: width / 2 - cx * k, y: height / 2 - cy * k };
    if (snap || !hasFit || REDUCED_MOTION) {
      view = target;
      viewAnim = null;
      hasFit = true;
      return;
    }
    // Skip imperceptible refits so a same-size rebuild does not start a
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

  function fadeOf(n, now) {
    if (!n.bornAt) return 1;
    return Math.max(0, Math.min(1, (now - n.bornAt) / 280));
  }

  function draw() {
    if (!ctx) return;
    const now = performance.now();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    visEdges.forEach((e) => {
      const a0 = byId.get(e.from);
      const b0 = byId.get(e.to);
      if (!isRevealed(a0.id) || !isRevealed(b0.id)) return;
      const fade = Math.min(fadeOf(a0, now), fadeOf(b0, now));
      if (fade <= 0) return;
      const a = toScreen(a0);
      const b = toScreen(b0);
      const incident = focusId && (e.from === focusId || e.to === focusId);
      ctx.strokeStyle = incident ? colors.accent : colors.line;
      ctx.globalAlpha = (focusId ? (incident ? 0.9 : 0.35) : 0.7) * fade;
      ctx.lineWidth = incident ? 1.6 : 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;

    sim.forEach((n) => {
      if (!isRevealed(n.id)) return;
      const fade = fadeOf(n, now);
      if (fade <= 0) return;
      const p = toScreen(n);
      const rFull = radiusOf(n);
      const scale = 0.6 + 0.4 * (1 - Math.pow(1 - fade, 3));
      const r = rFull * scale;
      ctx.globalAlpha = fade;
      ctx.fillStyle = nodeColor(n);
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = colors.paper;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.stroke();

      if (n.id === focusId) {
        ctx.globalAlpha = fade;
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2);
        ctx.stroke();
      }
    });

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    sim.forEach((n) => {
      if (!isRevealed(n.id)) return;
      const fade = fadeOf(n, now);
      if (fade <= 0) return;
      const p = toScreen(n);
      const r = radiusOf(n);
      const isFocus = n.id === focusId;
      ctx.font =
        (isFocus ? "600 12px " : "11px ") +
        '"Iowan Old Style", Palatino, Georgia, serif';
      const ty = p.y + r + 4;
      ctx.lineWidth = 3;
      ctx.strokeStyle = colors.paper;
      ctx.globalAlpha = fade;
      ctx.strokeText(n.name, p.x, ty);
      ctx.fillStyle = isFocus ? colors.ink : colors.dim;
      ctx.fillText(n.name, p.x, ty);
    });
    ctx.globalAlpha = 1;
  }

  function requestDraw() {
    if (!rafId) draw();
  }

  let fadeUntil = 0;

  // Stamp any not-yet-revealed node with a birth time as it enters
  // `revealedIds`, then run a short rAF loop that only redraws for the
  // opacity/scale fade. Layout is already settled; this never moves anything.
  function startReveal() {
    if (REDUCED_MOTION) {
      sim.forEach((n) => delete n.bornAt);
      draw();
      return;
    }
    const now = performance.now();
    let i = 0;
    sim.forEach((n) => {
      if (isRevealed(n.id) && n.bornAt == null && n.isNew !== false) {
        // isNew is left true until first reveal, so a node that arrives
        // already in `revealedIds` (or with revealedIds=null) still fades in
        // once, and a node that arrives unrevealed only fades on its later
        // reveal instead of retroactively.
        n.bornAt = now + Math.min(i * 14, 350);
        n.isNew = false;
        i++;
      }
    });
    fadeUntil = Math.max(fadeUntil, now + 320 + Math.min(i * 14, 350));
    if (i > 0) {
      if (!rafId) rafId = requestAnimationFrame(fadeFrame);
    } else {
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

  function resize() {
    if (!canvasEl) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = canvasEl.clientWidth;
    height = canvasEl.clientHeight;
    canvasEl.width = width * dpr;
    canvasEl.height = height * dpr;
    // Container geometry changed: re-fit instantly (a resize is not a
    // content change, easing here would look like lag).
    settleAndFit(true);
    requestDraw();
  }

  onMount(() => {
    ctx = canvasEl.getContext("2d");
    resize();
    refreshColors();
    startReveal();

    const ro = new ResizeObserver(() => resize());
    ro.observe(stageEl);

    const root = canvasEl.closest(".grimoire");
    let mo = null;
    if (root) {
      mo = new MutationObserver(() => refreshColors());
      mo.observe(root, { attributes: true, attributeFilter: ["class"] });
    }

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      ro.disconnect();
      mo?.disconnect();
    };
  });
</script>

<div class="mini-stage" bind:this={stageEl}>
  <canvas bind:this={canvasEl} aria-hidden="true"></canvas>
</div>

<style>
  .mini-stage {
    position: relative;
    width: 100%;
    height: 100%;
  }

  canvas {
    display: block;
    width: 100%;
    height: 100%;
  }
</style>
