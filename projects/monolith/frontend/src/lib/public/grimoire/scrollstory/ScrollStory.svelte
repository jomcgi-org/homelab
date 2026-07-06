<script>
  // "From scan to query": the public Grimoire landing's scroll-scrubbed
  // explainer. A tall scroller wraps a position: sticky full-viewport stage;
  // one rAF loop maps scroll progress to a master fraction t in [0, 1], and the
  // pure timeline.js module turns t into per-element layouts that we apply
  // imperatively to plain (non-reactive) element refs. This is the exact
  // ExploreCanvas discipline: 60fps writes must not go through Svelte's
  // reactivity proxy, so only the coarse phase id and a couple of open/closed
  // flags are runes; everything animated lives on plain refs mutated in frame().
  //
  // Faithful port of the approved reference mockup
  // (./reference-mockup.html, 9 iterations with the reviewer). Two deliberate
  // forks for the repo: colors come from --grim-type-* tokens (not the mockup's
  // hard-coded hexes) so dark mode works, and the graph layout is computed at
  // module eval (pure, no DOM) so the no-JS / reduced-motion static scene can
  // render the same graph server-side.
  import { onMount } from "svelte";
  import {
    PHASES,
    phaseAt,
    sub,
    lerp,
    ease,
    outCubic,
    outQuart,
    inOutCubic,
    outBack,
    outExpo,
    segmentize,
    graphLayout,
  } from "./timeline.js";
  import * as story from "./data/story.js";
  import { transcript, isPlaceholder } from "./data/transcript.js";
  import {
    libraryHref,
    exploreHref,
    chatHref,
  } from "$lib/public/grimoire/api.js";

  const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  // The captured answer carries "\n\n" paragraph breaks; HTML collapses raw
  // newlines to a space, so turn them into <br> for the chat bubble.
  const nl2br = (s) => s.replace(/\n/g, "<br>");
  const typeVar = (type) => `var(--grim-type-${type})`;

  // ── Pure, SSR-safe derived data (module eval; runs on the server too) ──
  const entById = Object.fromEntries(story.entities.map((e) => [e.id, e]));
  const nodes = graphLayout(story.entities, story.edges);
  const nById = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const graphEdges = story.edges.filter((e) => nById[e.from] && nById[e.to]);

  // Every real highlightable phrase on the page: entity names plus recorded
  // mention texts, each carrying its type's token color. segmentize() sorts
  // them longest-first and marks non-overlapping occurrences. Nothing invented.
  const PHRASES = [
    ...story.entities.map((e) => ({
      phrase: e.name,
      color: typeVar(e.type),
    })),
    ...story.mentions
      .filter((m) => m.text && entById[m.entity])
      .map((m) => ({
        phrase: m.text,
        color: typeVar(entById[m.entity].type),
      })),
  ];

  const segHtml = (segs) =>
    segs
      .map((s) =>
        s.c ? `<mark data-c="${s.c}">${esc(s.t)}</mark>` : esc(s.t),
      )
      .join("");

  // Chunk cards: section breadcrumb + highlighted body. bodyHtml is built once
  // and injected with {@html}: the source is baked corpus text (not user
  // input) and every segment is escaped, so the only markup is our own <mark>
  // tags. This keeps the reassembled text byte-exact, which matters because the
  // per-frame mark tinting reads data-c off these same nodes.
  const cards = story.chunks.map((c) => ({
    id: c.id,
    path: c.section.split("/").pop(),
    bodyHtml: segHtml(segmentize(c.content.slice(0, 240), PHRASES)),
  }));

  // Chat answer, segmented once. The animated scene types it out by slicing
  // these segments (renderTypedAnswer); the static scene shows it whole.
  const answerSegs = segmentize(transcript.answer, PHRASES);
  const answerHtml = answerSegs
    .map((s) => {
      const frag = nl2br(esc(s.t));
      return s.c ? `<mark data-c="${s.c}">${frag}</mark>` : frag;
    })
    .join("");

  // Grounded-in chips: resolve each cited name to a real page node so its
  // graph node can pulse when the citation appears.
  const groundedNodes = transcript.groundedIn
    .map((name) =>
      nodes.find((n) => n.name.toLowerCase().includes(name.toLowerCase())),
    )
    .filter(Boolean);

  const COUNTS = [
    ["books", story.corpus.books],
    ["chunks", story.corpus.chunks],
    ["entities", story.corpus.entities],
    ["relationships", story.corpus.edges],
  ];
  const topTypes = Object.entries(story.corpus.byType)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 9);

  const CAPTIONS = {
    layout: [
      "1 / LAYOUT DETECTION",
      "Marker reads the scanned page and finds its structure: headers, columns, asides, art.",
    ],
    chunks: [
      "2 / STRUCTURAL CHUNKING",
      "Blocks become chunks in reading order, keyed to the section they belong to.",
    ],
    entities: [
      "3 / ENTITY EXTRACTION",
      "An LLM reads each chunk and emits typed entities, and how they relate. Click a node.",
    ],
  };

  const attribution =
    "Lost Mine of Phandelver, p.50 · Wizards of the Coast · excerpt shown for demonstration";
  const aspect = story.source.aspect;

  // ── Reactive state (coarse only) ──
  let ready = $state(false); // scrubbed path is live (JS on, motion allowed)
  let reduced = $state(false);
  let phaseId = $state("hero");
  let popIdx = $state(-1);

  // Pop-out card content follows popIdx reactively; its screen position is set
  // imperatively every frame (placePopout) so it tracks the moving node.
  const popData = $derived.by(() => {
    if (popIdx < 0) return null;
    const n = nodes[popIdx];
    const rels = graphEdges
      .filter((e) => e.from === n.id || e.to === n.id)
      .slice(0, 6)
      .map((e) => {
        const other = nById[e.from === n.id ? e.to : e.from];
        return { rel: e.type.replace(/_/g, " "), name: other?.name ?? "?" };
      });
    const nMentions = story.mentions.filter((m) => m.entity === n.id).length;
    return { name: n.name, type: n.type, rels, nMentions };
  });

  // ── Plain (non-reactive) per-frame state ──
  let scrollerEl, stageEl, canvasEl, pageWrapEl;
  let heroCopyEl, captionEl, capNumEl, capTxtEl;
  let chunksWrapEl, chipsWrapEl, edgesSvgEl, popoutEl;
  let scalePanelEl, typeChipsEl, chatHeadEl, chatEl, bubbleAEl, groundedEl, ctasEl;
  let boxEls = [];
  let cardEls = [];
  let markEls = [];
  let chipEls = [];
  let lineEls = [];
  let counterEls = [];
  let snapEls = [];

  let ctx = null;
  let dpr = 1;
  let W = 0;
  let H = 0;
  let pageW = 0;
  let pageH = 0;
  let lastT = 0;
  let popT = 0;
  let popAnchor = null;
  let raf = 0;
  let measured = false;
  let dots = [];
  const lastNodePos = [];

  // Canvas can't read CSS custom properties, so resolve the --grim-type-*
  // tokens to concrete colors off the (in-.grimoire) canvas element, and
  // re-resolve when the theme class flips. Same trick as ExploreCanvas.
  let typeColors = {};
  let faintColor = "#8a94a2";
  function refreshTypeColors() {
    if (!canvasEl) return;
    const cs = getComputedStyle(canvasEl);
    faintColor = cs.getPropertyValue("--grim-text-faint").trim() || faintColor;
    const next = {};
    for (const t of Object.keys(story.corpus.byType)) {
      next[t] = cs.getPropertyValue(`--grim-type-${t}`).trim() || faintColor;
    }
    typeColors = next;
  }
  const dotColor = (t) => typeColors[t] || faintColor;

  const fmt = (x) => Math.round(x).toLocaleString("en-GB");

  // Seeded PRNG (mulberry32) for the constellation, so SSR/CSR and repeat
  // visits render the same starfield.
  function mulberry32(a) {
    return () => {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function buildDots() {
    const crand = mulberry32(7);
    const typePool = [];
    for (const [t, n] of Object.entries(story.corpus.byType)) {
      const count = Math.round((n / story.corpus.entities) * 1800);
      for (let i = 0; i < count; i++) typePool.push(t);
    }
    dots = typePool
      .map(() => {
        const r = Math.sqrt(crand());
        const th = crand() * Math.PI * 2;
        return {
          x: 0.5 + r * Math.cos(th) * 0.58,
          y: 0.5 + r * Math.sin(th) * 0.5,
          r,
          s: 1.2 + crand() * 1.8,
          t: typePool[Math.floor(crand() * typePool.length)],
        };
      })
      .sort((a, b) => a.r - b.r);
  }

  function renderTypedAnswer(chars) {
    let html = "";
    let used = 0;
    for (const s of answerSegs) {
      if (used >= chars) break;
      const frag = nl2br(esc(s.t.slice(0, chars - used)));
      html += s.c
        ? `<span style="color:${s.c};font-weight:600">${frag}</span>`
        : frag;
      used += s.t.length;
    }
    return html;
  }

  // ── pop-out open/close ──
  function togglePopout(i, anchor = null) {
    if (popIdx === i) {
      closePopout();
      return;
    }
    popIdx = i;
    popAnchor = anchor;
    popT = lastT;
    requestAnimationFrame(placePopout);
  }
  function closePopout() {
    popIdx = -1;
    popAnchor = null;
  }
  function placePopout() {
    if (popIdx < 0 || !popoutEl) return;
    const [x, y] = popAnchor || lastNodePos[popIdx] || [W / 2, H / 2];
    popoutEl.style.left = clamp(x + 16, 12, W - 300) + "px";
    popoutEl.style.top = clamp(y + 14, 12, H - 240) + "px";
  }

  function railTo(p) {
    if (!scrollerEl) return;
    const span = scrollerEl.offsetHeight - window.innerHeight;
    window.scrollTo({
      top: scrollerEl.offsetTop + p.rest * span,
      behavior: "smooth",
    });
  }

  // ── layout: viewport-dependent geometry, recomputed on resize ──
  function layout() {
    W = window.innerWidth;
    H = window.innerHeight;
    pageH = H * 0.76;
    pageW = pageH * aspect;
    if (pageW > W * 0.42) {
      pageW = W * 0.42;
      pageH = pageW / aspect;
    }
    pageWrapEl.style.width = pageW + "px";
    pageWrapEl.style.height = pageH + "px";
    pageWrapEl.style.left = W * 0.56 - pageW / 2 + "px";
    pageWrapEl.style.top = (H - pageH) / 2 + "px";
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvasEl.width = W * dpr;
    canvasEl.height = H * dpr;
    canvasEl.style.width = W + "px";
    canvasEl.style.height = H + "px";
    edgesSvgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const span = scrollerEl.offsetHeight - H;
    snapEls.forEach((s, i) => {
      if (s) s.style.top = PHASES[i].rest * span + "px";
    });
  }

  // Graph node screen position: middle band during its phase, shrinking to a
  // bright cluster during the pull-back, drifting upper-left for chat.
  function nodePos(n, shrink, chatP = 0) {
    const cx = W * 0.54;
    const cy = H * 0.52;
    const R = Math.min(W * 0.4, H * 0.42);
    let sx = cx + n.x * R;
    let sy = cy + n.y * R * 0.78;
    // exclusion zone: keep nodes clear of the top-left caption block
    if (sy < H * 0.32 && sx < W * 0.38) sx = W * 0.38 + (W * 0.38 - sx) * 0.2;
    const kx = lerp(cx, W * 0.16, chatP);
    const ky = lerp(H * 0.78, H * 0.44, chatP);
    return [
      lerp(sx, kx + n.x * R * 0.12, shrink),
      lerp(sy, ky + n.y * R * 0.07, shrink),
    ];
  }

  // ── the per-frame choreography (ported verbatim from the mockup) ──
  function frame(t) {
    const phase = phaseAt(t);
    if (phase.id !== phaseId) phaseId = phase.id;
    const interactive =
      phase.id === "entities" || phase.id === "scale" || phase.id === "chat";
    if (popIdx >= 0 && (!interactive || Math.abs(t - popT) > 0.012))
      closePopout();
    chipsWrapEl.style.pointerEvents =
      phase.id === "entities" || phase.id === "scale" ? "auto" : "none";

    // hero copy
    const pHero = sub(t, 0.04, 0.08);
    heroCopyEl.style.opacity = 1 - pHero;
    heroCopyEl.style.transform = `translateY(${-pHero * 30}px)`;
    heroCopyEl.style.visibility = pHero >= 1 ? "hidden" : "visible";

    // page transform
    const settle = outCubic(sub(t, 0, 0.08));
    const slide = inOutCubic(sub(t, 0.26, 0.36));
    const recede = outCubic(sub(t, 0.44, 0.54));
    const gone = inOutCubic(sub(t, 0.66, 0.72));
    const rot = lerp(-3.5, 0, settle);
    const px = lerp(lerp(0, -W * 0.34, slide), -W * 0.52, gone);
    const sc = lerp(lerp(lerp(0.94, 1, settle), 0.78, slide), 0.66, recede);
    pageWrapEl.style.transform = `translate(${px}px,0) rotate(${rot}deg) scale(${sc})`;
    pageWrapEl.style.opacity = lerp(lerp(1, 0.25, recede), 0, gone);

    // layout boxes: crisp staggered scanner strokes, then they fly to cards
    const pLay = sub(t, 0.08, 0.2);
    const flyP = inOutCubic(sub(t, 0.27, 0.38));
    story.bboxes.forEach((b, i) => {
      const el = boxEls[i];
      if (!el) return;
      const dp = outQuart(
        sub(
          pLay,
          (i / story.bboxes.length) * 0.7,
          (i / story.bboxes.length) * 0.7 + 0.22,
        ),
      );
      if (flyP <= 0) {
        el.style.opacity = dp;
        el.style.transform = `scaleY(${lerp(0.25, 1, dp)})`;
      } else {
        el.style.opacity = Math.max(0, 1 - flyP * (b.chunkId ? 1.4 : 3));
        el.style.transform = b.chunkId ? `translateX(${flyP * 60}px)` : "none";
      }
    });

    // chunk cards in, then out once chips have extracted
    const pChunk = sub(t, 0.28, 0.38);
    const cardsOut = inOutCubic(sub(t, 0.5, 0.58));
    cardEls.forEach((el, i) => {
      if (!el) return;
      const dp = outCubic(sub(pChunk, i * 0.08, i * 0.08 + 0.45));
      el.style.opacity = dp * (1 - cardsOut);
      el.style.transform = `translateX(${lerp(80, 0, dp) + cardsOut * 60}px)`;
    });
    chunksWrapEl.style.visibility = cardsOut >= 1 ? "hidden" : "visible";

    // mention marks tint at the start of the entities phase
    const pMark = ease(sub(t, 0.44, 0.48));
    markEls.forEach((m) => {
      const c = m.dataset.c;
      m.style.background =
        pMark > 0
          ? `color-mix(in srgb, ${c} ${Math.round(pMark * 22)}%, transparent)`
          : "transparent";
      m.style.color = pMark > 0.5 ? c : "inherit";
      m.style.fontWeight = pMark > 0.5 ? "700" : "inherit";
    });

    // chips fly from the card column to their graph nodes; edges draw in
    const pChip = sub(t, 0.47, 0.56);
    const shrink = inOutCubic(sub(t, 0.66, 0.74));
    const graphDim = outCubic(sub(t, 0.82, 0.88));
    const chatShift = inOutCubic(sub(t, 0.82, 0.9));
    nodes.forEach((n, i) => {
      const el = chipEls[i];
      if (!el) return;
      const raw = sub(pChip, i * 0.025, i * 0.025 + 0.4);
      const move = inOutCubic(raw);
      const pop = outBack(raw);
      const [gx, gy] = nodePos(n, shrink, chatShift);
      lastNodePos[i] = [gx, gy];
      const x = lerp(W * 0.74, gx, move);
      const y = lerp(H * 0.5, gy, move);
      el.style.opacity =
        Math.min(raw * 3, 1) * lerp(1, 0.4, Math.max(shrink * 0.5, graphDim));
      el.style.transform = `translate(${x}px,${y}px) translate(-50%,-50%) scale(${
        lerp(0.4, 1, pop) * lerp(1, 0.66, shrink)
      })`;
    });
    placePopout();
    const pEdge = outCubic(sub(t, 0.52, 0.6));
    lineEls.forEach((l, i) => {
      if (!l) return;
      const a = nById[graphEdges[i].from];
      const b = nById[graphEdges[i].to];
      const [x1, y1] = nodePos(a, shrink, chatShift);
      const [x2, y2] = nodePos(b, shrink, chatShift);
      l.setAttribute("x1", x1);
      l.setAttribute("y1", y1);
      l.setAttribute("x2", x2);
      l.setAttribute("y2", y2);
      const dp = clamp(pEdge * lineEls.length - i * 0.5, 0, 1);
      l.style.strokeOpacity = 0.45 * dp * lerp(1, 0.25, graphDim);
    });

    // scale: constellation + counters + type breakdown
    const pScale = sub(t, 0.66, 0.76);
    const chatDim = ease(sub(t, 0.82, 0.88));
    if (ctx) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (pScale > 0) {
        const vis = Math.floor(ease(pScale) * dots.length);
        const alpha = lerp(1, 0.3, chatDim);
        for (let i = 0; i < vis; i++) {
          const d = dots[i];
          const x = d.x * W;
          const y = d.y * H;
          const edge = clamp(Math.min(x, W - x, y, H - y) / 90, 0, 1);
          // keep the progress rail's column clear
          if (x > W - 150 && y > H * 0.26 && y < H * 0.74) continue;
          if (edge <= 0) continue;
          ctx.globalAlpha = 0.72 * alpha * edge;
          ctx.fillStyle = dotColor(d.t);
          ctx.beginPath();
          ctx.arc(x, y, d.s, 0, 7);
          ctx.fill();
        }
        ctx.globalAlpha = 1;
      }
    }
    const pCount = outExpo(sub(t, 0.68, 0.76));
    const panelIn = outCubic(sub(t, 0.665, 0.71));
    scalePanelEl.style.opacity = panelIn * (1 - chatDim);
    scalePanelEl.style.transform = `translateX(-50%) translateY(${lerp(18, 0, panelIn)}px)`;
    counterEls.forEach((el, i) => {
      if (el) el.textContent = fmt(COUNTS[i][1] * pCount);
    });
    typeChipsEl.style.opacity = ease(sub(t, 0.72, 0.76));

    // chat: headline fades in up top, panel rises to below it, answer types out
    const pChat = sub(t, 0.82, 1);
    const rise = outCubic(sub(pChat, 0, 0.14));
    chatHeadEl.style.opacity = rise * ease(sub(pChat, 0.04, 0.16));
    chatEl.style.transform = `translate(-50%, calc(-44% + ${lerp(120, 0, rise)}vh))`;
    const typed = Math.floor(
      transcript.answer.length * clamp((pChat - 0.18) / 0.5, 0, 1),
    );
    bubbleAEl.innerHTML =
      renderTypedAnswer(typed) +
      (typed > 0 && typed < transcript.answer.length
        ? '<span class="caret"></span>'
        : "");
    const pG = ease(sub(pChat, 0.7, 0.8));
    groundedEl.style.opacity = pG;
    groundedNodes.forEach((n) => {
      const i = nodes.indexOf(n);
      if (!chipEls[i]) return;
      chipEls[i].classList.toggle("lit", pG > 0.5);
      if (pG > 0.5) chipEls[i].style.opacity = 0.95;
    });
    ctasEl.style.opacity = ease(sub(pChat, 0.82, 0.92));

    // caption (top-left, crossfades between phases)
    const cap = CAPTIONS[phase.id];
    if (cap) {
      const pin = sub(t, phase.start + 0.005, phase.start + 0.025);
      const pout =
        phase.id === "chat" ? 0 : sub(t, phase.end - 0.012, phase.end);
      captionEl.style.opacity = Math.min(pin, 1 - pout);
      capNumEl.textContent = cap[0];
      capTxtEl.textContent = cap[1];
    } else {
      captionEl.style.opacity = 0;
    }
  }

  function onFrame() {
    raf = 0;
    if (!measured) return; // wait for the first layout() before scrubbing
    const r = scrollerEl.getBoundingClientRect();
    const span = r.height - H;
    lastT = span > 0 ? clamp(-r.top / span, 0, 1) : 0;
    frame(lastT);
  }

  onMount(() => {
    reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return; // stay on the static stacked scenes

    ready = true; // reveal the scrubbed stage (re-render, refs already bound)
    buildDots();
    ctx = canvasEl.getContext("2d");
    refreshTypeColors();

    const queue = () => {
      if (!raf) raf = requestAnimationFrame(onFrame);
    };
    const onResize = () => {
      layout();
      queue();
    };
    const onDocClick = () => closePopout();
    const onKey = (e) => {
      if (e.key === "Escape") closePopout();
    };
    window.addEventListener("scroll", queue, { passive: true });
    window.addEventListener("resize", onResize, { passive: true });
    document.addEventListener("click", onDocClick);
    window.addEventListener("keydown", onKey);

    const root = canvasEl.closest(".grimoire");
    let mo = null;
    if (root) {
      mo = new MutationObserver(() => {
        refreshTypeColors();
        queue();
      });
      mo.observe(root, { attributes: true, attributeFilter: ["class"] });
    }

    // The stage is displayed now (ready flipped): measure and paint the first
    // frame after the browser has laid it out.
    requestAnimationFrame(() => {
      markEls = [...stageEl.querySelectorAll(".chunk-card mark")];
      layout();
      measured = true;
      onFrame();
    });

    return () => {
      window.removeEventListener("scroll", queue);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("click", onDocClick);
      window.removeEventListener("keydown", onKey);
      mo?.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  });
</script>

<div class="scrollstory" class:ready>
  <!-- ── Scrubbed stage: always in the DOM (refs bind at mount) but hidden by
       CSS until `ready` flips, so no-JS and reduced-motion never see it ── -->
  <div class="scroller" bind:this={scrollerEl}>
    {#each PHASES as p, i (p.id)}
      <div class="snap" bind:this={snapEls[i]}></div>
    {/each}

    <div class="stage" bind:this={stageEl}>
      <canvas class="constellation" bind:this={canvasEl} aria-hidden="true"
      ></canvas>

      <div class="hero-copy" bind:this={heroCopyEl}>
        <h1 class="grim-title">
          <span class="line">Grimoire.</span><span class="line accent"
            >From scan to query.</span
          >
        </h1>
        <p>
          Watch one page of Lost Mine of Phandelver become structured, queryable
          knowledge: layout, chunks, entities, and a grounded answer.
        </p>
        <div class="cue">SCROLL <span class="arrow">&darr;</span></div>
      </div>

      <div class="page-wrap" bind:this={pageWrapEl}>
        <img
          src={story.image}
          alt="Scanned book page: Nezznar the Black Spider"
        />
        <div class="overlay">
          {#each story.bboxes as b, i (b.id)}
            <div
              class="bbox k-{b.kind}"
              bind:this={boxEls[i]}
              style="left:{b.x * 100}%;top:{b.y * 100}%;width:{b.w *
                100}%;height:{b.h * 100}%"
            ></div>
          {/each}
        </div>
        <div class="attribution">{attribution}</div>
      </div>

      <div class="chunks" bind:this={chunksWrapEl}>
        {#each cards as c, i (c.id)}
          <div class="chunk-card" bind:this={cardEls[i]}>
            <div class="path">{c.path}</div>
            <div class="body">{@html c.bodyHtml}</div>
          </div>
        {/each}
      </div>

      <div class="graph">
        <svg class="edges" bind:this={edgesSvgEl} aria-hidden="true">
          {#each graphEdges as e, i (i)}
            <line bind:this={lineEls[i]} />
          {/each}
        </svg>
        <div class="chips" bind:this={chipsWrapEl}>
          {#each nodes as n, i (n.id)}
            <button
              class="chip"
              class:open={popIdx === i}
              bind:this={chipEls[i]}
              onclick={(ev) => {
                ev.stopPropagation();
                togglePopout(i);
              }}
            >
              <span class="dot" style="background:{typeVar(n.type)}"></span
              >{n.name}
            </button>
          {/each}
        </div>
      </div>

      <!-- outside .graph so it stacks above the chat panel (a child z-index
           cannot escape its parent's stacking context) -->
      <div class="popout" class:on={popIdx >= 0} bind:this={popoutEl}>
        {#if popData}
          <span
            class="ptype"
            style="color:{typeVar(popData.type)};background:color-mix(in srgb, {typeVar(
              popData.type,
            )} 12%, transparent)">{popData.type}</span
          >
          <div class="pname grim-title">{popData.name}</div>
          <ul class="prels">
            {#each popData.rels as r (r.rel + r.name)}
              <li><span class="rel">{r.rel}</span> {r.name}</li>
            {:else}
              <li><span class="rel">no relationships on this page</span></li>
            {/each}
          </ul>
          <div class="pfoot">
            extracted from {popData.nMentions} mention{popData.nMentions === 1
              ? ""
              : "s"} on this page
          </div>
        {/if}
      </div>

      <div class="scale-panel" bind:this={scalePanelEl}>
        <div class="scale-head grim-title">One page in. The whole shelf follows.</div>
        <div class="this-page">
          <span class="k">THIS PAGE</span>
          <span>{story.bboxes.length} blocks</span><span class="k">/</span>
          <span>{story.chunks.length} chunks</span><span class="k">/</span>
          <span>{story.entities.length} entities</span><span class="k">/</span>
          <span>{graphEdges.length} relationships</span>
          <span class="arrow">&darr;</span><span class="k">THE SHELF</span>
        </div>
        <div class="counters">
          {#each COUNTS as [label], i (label)}
            <div class="counter">
              <div class="n" bind:this={counterEls[i]}>0</div>
              <div class="l">{label}</div>
            </div>
          {/each}
        </div>
        <div class="type-chips" bind:this={typeChipsEl}>
          {#each topTypes as [t, n] (t)}
            <span class="tchip">
              <span class="dot" style="background:{typeVar(t)}"></span>{t}
              <span class="n">{n.toLocaleString("en-GB")}</span>
            </span>
          {/each}
        </div>
      </div>

      <div class="chat-head" bind:this={chatHeadEl}>
        <div class="scale-head grim-title">Ask the Grimoire.</div>
        <div class="chat-sub">
          Every claim cites the chunks and entities it came from. Click a
          citation.
        </div>
      </div>

      <div class="chat" bind:this={chatEl}>
        {#if isPlaceholder}
          <div class="mock-note">mock transcript</div>
        {/if}
        <div class="bubble-q">{transcript.question}</div>
        <div class="bubble-a" bind:this={bubbleAEl}></div>
        <div class="grounded" bind:this={groundedEl}>
          <span class="lbl">GROUNDED IN</span>
          {#each groundedNodes as n (n.id)}
            <button
              class="gchip"
              onclick={(ev) => {
                ev.stopPropagation();
                const r = ev.currentTarget.getBoundingClientRect();
                togglePopout(nodes.indexOf(n), [r.left, r.top - 250]);
              }}
            >
              <span class="dot" style="background:{typeVar(n.type)}"></span
              >{n.name}
            </button>
          {/each}
        </div>
        <div class="ctas" bind:this={ctasEl}>
          <a class="cta primary" href={chatHref()}>Ask the Grimoire</a>
          <a class="cta ghost" href={exploreHref()}>Wander the graph</a>
          <a class="cta ghost" href={libraryHref()}>Browse the library</a>
        </div>
      </div>

      <div class="caption" bind:this={captionEl}>
        <div class="num" bind:this={capNumEl}></div>
        <div class="txt" bind:this={capTxtEl}></div>
      </div>
    </div>
  </div>

  <nav class="rail" aria-label="Story sections">
    {#each PHASES as p (p.id)}
      <button class:on={phaseId === p.id} onclick={() => railTo(p)}>
        <span class="lbl">{p.label}</span>
      </button>
    {/each}
  </nav>

  <!-- ── Static stacked scenes: the reduced-motion / no-JS fallback, and the
       SSR-first render. Full content parity with the scrubbed version. ── -->
  <div class="static-story">
    <section class="static-hero">
      <h1 class="grim-title">
        <span class="line">Grimoire.</span><span class="line accent"
          >From scan to query.</span
        >
      </h1>
      <p>
        One page of Lost Mine of Phandelver, turned into structured, queryable
        knowledge: layout, chunks, entities, a graph, and a grounded answer.
      </p>
    </section>

    <section class="static-scene">
      <p class="static-cap"><b>1 / Layout detection.</b> Marker reads the scanned page and finds its structure: headers, columns, asides, art.</p>
      <div class="static-page" style="aspect-ratio:{aspect}">
        <img src={story.image} alt="Scanned book page: Nezznar the Black Spider" />
        <div class="overlay">
          {#each story.bboxes as b (b.id)}
            <div
              class="bbox k-{b.kind}"
              style="left:{b.x * 100}%;top:{b.y * 100}%;width:{b.w *
                100}%;height:{b.h * 100}%"
            ></div>
          {/each}
        </div>
      </div>
      <p class="static-attr">{attribution}</p>
    </section>

    <section class="static-scene">
      <p class="static-cap"><b>2 / Structural chunking.</b> Blocks become chunks in reading order, keyed to the section they belong to.</p>
      <div class="static-cards">
        {#each cards as c (c.id)}
          <div class="chunk-card">
            <div class="path">{c.path}</div>
            <div class="body">{@html c.bodyHtml}</div>
          </div>
        {/each}
      </div>
    </section>

    <section class="static-scene">
      <p class="static-cap"><b>3 / Entity extraction.</b> An LLM reads each chunk and emits typed entities and how they relate.</p>
      <svg class="static-graph" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Relationship graph of the entities on this page">
        {#each graphEdges as e, i (i)}
          <line
            x1={50 + nById[e.from].x * 40}
            y1={50 + nById[e.from].y * 32}
            x2={50 + nById[e.to].x * 40}
            y2={50 + nById[e.to].y * 32}
          />
        {/each}
        {#each nodes as n (n.id)}
          <circle cx={50 + n.x * 40} cy={50 + n.y * 32} r="1.4" fill={typeVar(n.type)} />
        {/each}
      </svg>
      <ul class="static-roster">
        {#each nodes as n (n.id)}
          <li>
            <span class="dot" style="background:{typeVar(n.type)}"></span>{n.name}
          </li>
        {/each}
      </ul>
    </section>

    <section class="static-scene">
      <p class="static-cap"><b>4 / The whole shelf.</b> You just watched one page. Every book on the shelf gets the same treatment.</p>
      <div class="static-counters">
        {#each COUNTS as [label, value] (label)}
          <div class="counter">
            <div class="n">{value.toLocaleString("en-GB")}</div>
            <div class="l">{label}</div>
          </div>
        {/each}
      </div>
      <div class="type-chips">
        {#each topTypes as [t, n] (t)}
          <span class="tchip">
            <span class="dot" style="background:{typeVar(t)}"></span>{t}
            <span class="n">{n.toLocaleString("en-GB")}</span>
          </span>
        {/each}
      </div>
    </section>

    <section class="static-scene">
      <p class="static-cap"><b>5 / Grounded answers.</b> Every claim cites the chunks and entities it came from.</p>
      <div class="chat static-chat">
        {#if isPlaceholder}
          <div class="mock-note">mock transcript</div>
        {/if}
        <div class="bubble-q">{transcript.question}</div>
        <div class="bubble-a">{@html answerHtml}</div>
        <div class="grounded static-grounded">
          <span class="lbl">GROUNDED IN</span>
          {#each groundedNodes as n (n.id)}
            <span class="gchip">
              <span class="dot" style="background:{typeVar(n.type)}"></span
              >{n.name}
            </span>
          {/each}
        </div>
        <div class="ctas static-ctas">
          <a class="cta primary" href={chatHref()}>Ask the Grimoire</a>
          <a class="cta ghost" href={exploreHref()}>Wander the graph</a>
          <a class="cta ghost" href={libraryHref()}>Browse the library</a>
        </div>
      </div>
    </section>
  </div>
</div>

<style>
  /* All colors are --grim-* tokens so light/dark both work; the scoped
     .grimoire ancestor (the app shell) resolves them. */

  /* By default (no JS, or reduced motion) the scrubbed stage is hidden and the
     static scenes show. onMount flips .ready only when motion is allowed. */
  .scroller,
  .rail {
    display: none;
  }
  .scrollstory.ready .scroller {
    display: block;
  }
  .scrollstory.ready .rail {
    display: flex;
  }
  .scrollstory.ready .static-story {
    display: none;
  }

  .scroller {
    height: 720vh;
    position: relative;
  }
  .snap {
    position: absolute;
    left: 0;
    width: 1px;
    height: 2px;
    scroll-snap-align: start;
  }
  .stage {
    position: sticky;
    top: 0;
    height: 100vh;
    overflow: hidden;
    background: var(--grim-paper);
  }

  /* hero copy */
  .hero-copy {
    position: absolute;
    left: 6vw;
    top: 26vh;
    max-width: 34ch;
    z-index: 6;
    will-change: transform, opacity;
  }
  .hero-copy h1 {
    font-size: clamp(34px, 4.6vw, 58px);
    line-height: 1.08;
    margin: 0;
    text-wrap: balance;
  }
  .hero-copy h1 .line {
    display: block;
  }
  .hero-copy h1 .accent {
    color: var(--grim-accent);
    font-size: 0.62em;
    margin-top: 6px;
  }
  .hero-copy p {
    color: var(--grim-text-dim);
    font-size: 14px;
    line-height: 1.6;
    margin-top: 16px;
    max-width: 38ch;
  }
  .cue {
    margin-top: 26px;
    color: var(--grim-text-faint);
    font-size: 11px;
    letter-spacing: 0.18em;
    font-family: var(--font-mono);
  }
  .cue .arrow {
    display: inline-block;
    animation: ss-nudge 1.6s ease-in-out infinite;
  }
  @keyframes ss-nudge {
    50% {
      transform: translateY(4px);
    }
  }

  /* caption: top-left, clear of the page/cards/chat lanes */
  .caption {
    position: absolute;
    left: 6vw;
    top: 6vh;
    z-index: 8;
    max-width: 38ch;
    opacity: 0;
    pointer-events: none;
    padding: 12px 16px 14px;
    margin: -12px -16px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--grim-paper) 80%, transparent);
    backdrop-filter: blur(3px);
  }
  .caption .num {
    font-size: 11px;
    color: var(--grim-accent);
    font-weight: 700;
    letter-spacing: 0.16em;
    font-family: var(--font-mono);
  }
  .caption .txt {
    font-family: var(--grim-serif);
    font-size: clamp(15px, 1.5vw, 19px);
    line-height: 1.4;
    margin-top: 4px;
    color: var(--grim-ink);
  }

  /* the page scan */
  .page-wrap {
    position: absolute;
    z-index: 2;
    will-change: transform, opacity;
    filter: drop-shadow(0 18px 40px rgba(10, 14, 22, 0.4));
  }
  .page-wrap img {
    width: 100%;
    height: 100%;
    display: block;
    border-radius: 3px;
    /* light frame so the scan reads against the dark paper too */
    outline: 1px solid var(--grim-line);
  }
  .overlay {
    position: absolute;
    inset: 0;
  }
  .bbox {
    position: absolute;
    border: 1.5px solid;
    border-radius: 2px;
    opacity: 0;
    will-change: transform, opacity;
  }
  /* kind classes are k- prefixed: marker emits a "caption" kind that would
     otherwise collide with the .caption rail component */
  .bbox.k-header {
    border-color: var(--grim-type-creature);
    background: color-mix(in srgb, var(--grim-type-creature) 14%, transparent);
  }
  .bbox.k-text,
  .bbox.k-caption {
    border-color: var(--grim-type-spell);
    background: color-mix(in srgb, var(--grim-type-spell) 10%, transparent);
  }
  .bbox.k-aside {
    border-color: var(--grim-type-npc);
    background: color-mix(in srgb, var(--grim-type-npc) 14%, transparent);
  }
  .bbox.k-art {
    border-color: var(--grim-type-faction);
    background: color-mix(in srgb, var(--grim-type-faction) 10%, transparent);
  }
  .attribution {
    position: absolute;
    left: 2px;
    bottom: -24px;
    font-size: 10px;
    color: var(--grim-text-faint);
    white-space: nowrap;
    font-family: var(--font-mono);
  }

  /* chunk cards: right column, clear of the rail gutter */
  .chunks {
    position: absolute;
    right: max(6vw, 88px);
    top: 50%;
    transform: translateY(-50%);
    width: min(38vw, 480px);
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 3;
  }
  .chunk-card {
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 8px;
    padding: 10px 14px;
    opacity: 0;
    will-change: transform, opacity;
    box-shadow: 0 4px 14px rgba(10, 14, 22, 0.12);
  }
  .chunk-card .path {
    font-size: 9.5px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--grim-accent);
    font-weight: 700;
    font-family: var(--font-mono);
  }
  .chunk-card .body {
    font-family: var(--grim-serif);
    font-size: 12.5px;
    line-height: 1.45;
    color: var(--grim-text-dim);
    margin-top: 5px;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .chunk-card :global(mark) {
    background: transparent;
    color: inherit;
    border-radius: 3px;
    padding: 0 2px;
  }

  /* entity chips + graph */
  .graph {
    position: absolute;
    inset: 0;
    z-index: 4;
    pointer-events: none;
  }
  .edges {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }
  .edges :global(line) {
    stroke: var(--grim-text-faint);
    stroke-opacity: 0;
    stroke-width: 1;
  }
  .chip {
    position: absolute;
    left: 0;
    top: 0;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 3px 10px 3px 6px;
    font-size: 10.5px;
    font-weight: 600;
    opacity: 0;
    will-change: transform, opacity;
    white-space: nowrap;
    box-shadow: 0 2px 8px rgba(10, 14, 22, 0.16);
    cursor: pointer;
    color: var(--grim-ink);
    font-family: var(--font-mono);
  }
  .chip .dot,
  .tchip .dot,
  .gchip .dot,
  .static-roster .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
  }
  .chip.lit {
    box-shadow:
      0 0 0 3px color-mix(in srgb, var(--grim-accent) 35%, transparent),
      0 2px 8px rgba(10, 14, 22, 0.2);
  }
  .chip.open {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--grim-accent) 45%, transparent);
  }

  /* entity pop-out */
  .popout {
    position: absolute;
    z-index: 12;
    width: 280px;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 14px 44px rgba(10, 14, 22, 0.35);
    opacity: 0;
    pointer-events: none;
    transform: translateY(6px) scale(0.97);
    transition:
      opacity 0.18s ease,
      transform 0.18s ease;
  }
  .popout.on {
    opacity: 1;
    pointer-events: auto;
    transform: translateY(0) scale(1);
  }
  .popout .ptype {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    border-radius: 4px;
    padding: 2px 7px;
    font-family: var(--font-mono);
  }
  .popout .pname {
    font-size: 19px;
    font-weight: 600;
    margin-top: 7px;
    color: var(--grim-ink);
  }
  .popout .prels {
    margin: 10px 0 0;
    padding: 0;
    list-style: none;
  }
  .popout .prels li {
    font-size: 10.5px;
    color: var(--grim-text-dim);
    padding: 4px 0;
    border-top: 1px solid var(--grim-surface-2);
    display: flex;
    gap: 6px;
    align-items: baseline;
  }
  .popout .rel {
    color: var(--grim-text-faint);
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    flex: none;
    font-family: var(--font-mono);
  }
  .popout .pfoot {
    margin-top: 10px;
    font-size: 9.5px;
    color: var(--grim-text-faint);
  }

  /* scale phase: stat panel up top, constellation behind */
  .constellation {
    position: absolute;
    inset: 0;
    z-index: 1;
  }
  .scale-panel,
  .chat-head {
    position: absolute;
    left: 50%;
    top: 10vh;
    transform: translateX(-50%);
    z-index: 6;
    text-align: center;
    opacity: 0;
    width: min(92vw, 860px);
    padding: 24px 28px 22px;
    border-radius: 18px;
    background: color-mix(in srgb, var(--grim-paper) 78%, transparent);
    backdrop-filter: blur(3px);
    pointer-events: none;
  }
  .chat-head {
    top: 9vh;
    padding: 18px 28px 16px;
  }
  .scale-head {
    font-size: clamp(24px, 3.2vw, 40px);
    font-weight: 600;
    text-wrap: balance;
    color: var(--grim-ink);
  }
  .this-page {
    margin-top: 18px;
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--grim-text-dim);
    display: flex;
    justify-content: center;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    font-family: var(--font-mono);
  }
  .this-page .k {
    color: var(--grim-text-faint);
    font-weight: 700;
    font-size: 9px;
  }
  .this-page .arrow {
    color: var(--grim-accent);
    font-size: 14px;
  }
  .counters {
    display: flex;
    justify-content: center;
    gap: clamp(18px, 4.5vw, 58px);
    margin-top: 18px;
  }
  .counter {
    text-align: center;
  }
  .counter .n {
    font-family: var(--grim-serif);
    font-size: clamp(28px, 4vw, 50px);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--grim-ink);
  }
  .counter .l {
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--grim-text-dim);
    margin-top: 4px;
    font-family: var(--font-mono);
  }
  .type-chips {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    max-width: 640px;
    margin: 20px auto 0;
  }
  .tchip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 4px 12px 4px 7px;
    font-size: 11px;
    font-weight: 600;
    box-shadow: 0 2px 8px rgba(10, 14, 22, 0.12);
    color: var(--grim-ink);
    font-family: var(--font-mono);
  }
  .tchip .n {
    color: var(--grim-text-dim);
    font-variant-numeric: tabular-nums;
    font-weight: 400;
  }

  .chat-sub {
    margin-top: 8px;
    font-family: var(--grim-serif);
    font-size: clamp(14px, 1.6vw, 18px);
    color: var(--grim-text-dim);
  }

  .chat {
    position: absolute;
    left: 50%;
    top: 52%;
    transform: translate(-50%, 120vh);
    width: min(90vw, 720px);
    z-index: 7;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 16px;
    padding: 22px 26px 20px;
    box-shadow: 0 18px 60px rgba(10, 14, 22, 0.35);
    will-change: transform;
  }
  .bubble-q {
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    border-radius: 12px 12px 3px 12px;
    padding: 9px 13px;
    font-size: 13px;
    max-width: 80%;
    margin-left: auto;
    width: fit-content;
  }
  .bubble-a {
    font-family: var(--grim-serif);
    font-size: 15.5px;
    line-height: 1.55;
    margin-top: 14px;
    min-height: 8em;
    color: var(--grim-ink);
  }
  .bubble-a :global(.caret) {
    display: inline-block;
    width: 7px;
    height: 1em;
    background: var(--grim-accent);
    vertical-align: text-bottom;
    animation: ss-blink 0.9s steps(1) infinite;
  }
  @keyframes ss-blink {
    50% {
      opacity: 0;
    }
  }
  .grounded {
    margin-top: 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    opacity: 0;
  }
  .grounded .lbl {
    font-size: 9px;
    letter-spacing: 0.18em;
    color: var(--grim-text-faint);
    font-weight: 700;
    font-family: var(--font-mono);
  }
  .gchip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 2px 9px 2px 5px;
    font-size: 10px;
    font-weight: 600;
    background: var(--grim-surface-2);
    font-family: var(--font-mono);
    color: var(--grim-ink);
    cursor: pointer;
  }
  .gchip .dot {
    width: 7px;
    height: 7px;
  }
  .ctas {
    display: flex;
    gap: 10px;
    margin-top: 14px;
    opacity: 0;
  }
  .cta {
    flex: 1;
    text-align: center;
    padding: 10px 8px;
    border-radius: 7px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    text-decoration: none;
    font-family: var(--font-mono);
  }
  .cta.primary {
    background: var(--grim-accent);
    color: var(--grim-on-accent);
  }
  .cta.ghost {
    border: 1px solid var(--grim-line);
    color: var(--grim-accent);
  }
  .mock-note {
    position: absolute;
    right: 10px;
    top: 8px;
    font-size: 9px;
    color: var(--grim-text-faint);
    font-family: var(--font-mono);
  }

  /* progress rail */
  .rail {
    position: fixed;
    right: 18px;
    top: 50%;
    transform: translateY(-50%);
    z-index: 10;
    flex-direction: column;
    gap: 14px;
  }
  .rail button {
    all: unset;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-direction: row-reverse;
    font-family: var(--font-mono);
    font-size: 9px;
    letter-spacing: 0.14em;
    color: var(--grim-text-faint);
  }
  .rail button::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 50%;
    border: 1.5px solid var(--grim-text-faint);
    flex: none;
  }
  .rail button.on {
    color: var(--grim-accent);
    font-weight: 700;
  }
  .rail button.on::before {
    background: var(--grim-accent);
    border-color: var(--grim-accent);
  }
  .rail .lbl {
    opacity: 0;
    transition: opacity 0.15s;
  }
  .rail button:hover .lbl,
  .rail button.on .lbl {
    opacity: 1;
  }

  /* ── static stacked scenes ── */
  .static-story {
    max-width: 900px;
    margin: 0 auto;
    padding: 40px 28px 24px;
  }
  .static-hero {
    padding: 24px 0 12px;
  }
  .static-hero h1 {
    font-size: clamp(34px, 5vw, 54px);
    line-height: 1.08;
    margin: 0;
  }
  .static-hero h1 .line {
    display: block;
  }
  .static-hero h1 .accent {
    color: var(--grim-accent);
    font-size: 0.62em;
    margin-top: 6px;
  }
  .static-hero p {
    margin-top: 16px;
    max-width: 60ch;
    color: var(--grim-text-dim);
    font-size: 15px;
    line-height: 1.6;
  }
  .static-scene {
    padding: 28px 0;
    border-top: 1px solid var(--grim-line-soft);
  }
  .static-cap {
    margin: 0 0 18px;
    max-width: 64ch;
    color: var(--grim-text-dim);
    font-size: 14px;
    line-height: 1.55;
  }
  .static-cap b {
    color: var(--grim-ink);
  }
  .static-page {
    position: relative;
    width: min(460px, 100%);
    margin-bottom: 26px;
  }
  .static-attr {
    margin: 10px 0 0;
    font-size: 10px;
    color: var(--grim-text-faint);
    font-family: var(--font-mono);
  }
  .static-page img {
    width: 100%;
    height: 100%;
    display: block;
    border-radius: 3px;
    outline: 1px solid var(--grim-line);
  }
  .static-page .bbox {
    opacity: 1;
  }
  .static-cards {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }
  .static-cards .chunk-card {
    opacity: 1;
  }
  .static-graph {
    width: 100%;
    max-width: 560px;
    aspect-ratio: 1;
    display: block;
  }
  .static-graph line {
    stroke: var(--grim-text-faint);
    stroke-opacity: 0.4;
    stroke-width: 0.3;
  }
  .static-roster {
    list-style: none;
    margin: 16px 0 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .static-roster li {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 3px 10px 3px 7px;
    font-size: 10.5px;
    font-weight: 600;
    color: var(--grim-ink);
    font-family: var(--font-mono);
  }
  .static-counters {
    display: flex;
    flex-wrap: wrap;
    gap: clamp(18px, 5vw, 48px);
    margin-bottom: 18px;
  }
  .static-chat {
    position: static;
    transform: none;
    width: 100%;
    max-width: 640px;
  }
  .static-grounded,
  .static-ctas {
    opacity: 1;
  }

  /* ── responsive ── */
  @media (max-width: 700px) {
    .hero-copy {
      top: 14vh;
    }
    .chunks {
      width: 86vw;
      right: 7vw;
    }
    .caption {
      max-width: 70vw;
    }
    .type-chips {
      max-width: 86vw;
    }
    .static-cards {
      grid-template-columns: 1fr;
    }
  }
</style>
