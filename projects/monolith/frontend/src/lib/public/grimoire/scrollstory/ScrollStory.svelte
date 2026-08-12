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
  // hard-coded hexes) so the theme owns them, and the graph layout is computed at
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
    cardFootprints,
  } from "./timeline.js";
  import * as story from "./data/story.js";
  // Smaller (800px) variant of the page scan for a srcset: the full asset is
  // 1200px but the scan never displays wider than ~600px on either form factor,
  // so most devices download less than half the bytes (a real LCP + payload
  // win). bake-scrollstory.sh emits both. See the <img> srcset below.
  import pageImageSm from "./data/page-sm.webp";
  import { transcript, isPlaceholder } from "./data/transcript.js";
  import {
    libraryHref,
    worldHref,
    chatHref,
  } from "$lib/public/grimoire/api.js";

  const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const typeVar = (type) => `var(--grim-type-${type})`;

  // ── Pure, SSR-safe derived data (module eval; runs on the server too) ──
  const entById = Object.fromEntries(story.entities.map((e) => [e.id, e]));
  const nodes = graphLayout(story.entities, story.edges);
  const nById = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const idxById = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
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
      .map((s) => (s.c ? `<mark data-c="${s.c}">${esc(s.t)}</mark>` : esc(s.t)))
      .join("");

  // Chunk cards: section breadcrumb + highlighted body. bodyHtml is built once
  // and injected with {@html}: the source is baked corpus text (not user
  // input) and every segment is escaped, so the only markup is our own <mark>
  // tags. This keeps the reassembled text byte-exact, which matters because the
  // per-frame mark tinting reads data-c off these same nodes.
  const cards = story.chunks.map((c) => ({
    id: c.id,
    ref: c.ref,
    path: c.section.split("/").pop(),
    bodyHtml: segHtml(segmentize(c.content.slice(0, 240), PHRASES)),
  }));

  // Each card's source footprint on the page: the chunking scene flies the
  // card itself from this footprint to its column slot, text aboard.
  const cardSrc = cardFootprints(cards, story.bboxes);
  // ...and each bbox's owning card, so a chunk's outlines vanish with its lift
  const bboxCard = story.bboxes.map((b) =>
    cards.findIndex((c) => c.ref === b.chunkId),
  );

  // Chat answer as PARAGRAPHS of SENTENCES, each sentence pre-rendered with
  // its entity highlights. The chat scene fades sentences in with a stagger:
  // character-typing was tried and felt wrong against scroll scrubbing.
  // (unlike the chunk cards, whose marks are tinted per-frame, the answer's
  // entity highlights are always-on: color them inline at build time)
  const answerSents = [];
  transcript.answer.split("\n\n").forEach((para, pi) => {
    para.split(/(?<=[.!?])\s+/).forEach((sentence) => {
      answerSents.push({
        pi,
        html: segmentize(sentence, PHRASES)
          .map((s) =>
            s.c ? `<mark style="color:${s.c}">${esc(s.t)}</mark>` : esc(s.t),
          )
          .join(""),
      });
    });
  });

  // Grounded-in chips: resolve each cited name to a real page node so its
  // graph node can pulse when the citation appears.
  const groundedNodes = transcript.groundedIn
    .map((name) =>
      nodes.find((n) => n.name.toLowerCase().includes(name.toLowerCase())),
    )
    .filter(Boolean);

  // On a phone a whole page's worth of entity pills (a dozen-plus) cannot fit
  // legibly on the narrow stage: they spill off the edges and pile up on the
  // clamp lines. So the mobile entity graph renders a smaller, high-signal
  // subset laid out on its own: the grounded (answer) nodes first, then the
  // highest-degree others, capped at MOBILE_GRAPH_NODES. Desktop keeps all of
  // them. The subset is force-laid separately so the few nodes spread to fill
  // the space instead of huddling where the full graph happened to place them.
  const MOBILE_GRAPH_NODES = 7;
  const degreeById = {};
  for (const e of story.edges) {
    degreeById[e.from] = (degreeById[e.from] || 0) + 1;
    degreeById[e.to] = (degreeById[e.to] || 0) + 1;
  }
  const groundedIds = new Set(groundedNodes.map((n) => n.id));
  const mobileEntities = [
    ...story.entities.filter((e) => groundedIds.has(e.id)),
    ...story.entities
      .filter((e) => !groundedIds.has(e.id))
      .sort((a, b) => (degreeById[b.id] || 0) - (degreeById[a.id] || 0)),
  ].slice(0, MOBILE_GRAPH_NODES);
  const mobileIds = new Set(mobileEntities.map((e) => e.id));
  const mobileEdges = story.edges.filter(
    (e) => mobileIds.has(e.from) && mobileIds.has(e.to),
  );
  const mobileNodes = graphLayout(mobileEntities, mobileEdges);
  const mobileNById = Object.fromEntries(mobileNodes.map((n) => [n.id, n]));

  const COUNTS = [
    ["books", story.corpus.books],
    ["passages", story.corpus.chunks],
    ["characters, places and items", story.corpus.entities],
    ["relationships", story.corpus.edges],
  ];
  const topTypes = Object.entries(story.corpus.byType)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 9);

  const CAPTIONS = {
    layout: [
      "1 / Reading the page.",
      "The scan gets picked apart into headers, columns, asides and art.",
    ],
    chunks: [
      "2 / Broken into passages.",
      "The page becomes readable passages in the right order, each tagged with the section it came from.",
    ],
    entities: [
      "3 / Who and what is on the page.",
      "Every character, place and item gets picked out, along with how they connect.",
    ],
  };

  const attribution =
    "Lost Mine of Phandelver, p.50 · Wizards of the Coast · excerpt shown for demonstration";
  const aspect = story.source.aspect;

  // ── Reactive state (coarse only) ──
  let ready = $state(false); // scrubbed path is live (JS on, motion allowed)
  let reduced = $state(false);
  // Narrow/touch viewports get a dedicated portrait choreography: same phases,
  // same data, but a single centred column instead of the desktop two-column
  // stage (page centre, cards full width, a tall clamped constellation, a
  // bottom dot rail). Decided at mount, re-checked on resize. See the ported
  // reference-mockup-mobile.html for the design source.
  let mobile = $state(false);
  let phaseId = $state("hero");
  let popIdx = $state(-1);

  // The entity graph binds its chip/edge elements and all per-frame math to
  // these "view" collections, so switching mobile <-> desktop swaps the whole
  // rendered graph (fewer nodes on a phone) without special-casing every read.
  // Declared after `mobile` so the deriveds can read it.
  const viewNodes = $derived(mobile ? mobileNodes : nodes);
  const viewEdges = $derived(mobile ? mobileEdges : graphEdges);
  const viewNById = $derived(mobile ? mobileNById : nById);
  const viewIdxById = $derived(
    Object.fromEntries(viewNodes.map((n, i) => [n.id, i])),
  );

  // Pop-out card content follows popIdx reactively; its screen position is set
  // imperatively every frame (placePopout) so it tracks the moving node.
  const popData = $derived.by(() => {
    if (popIdx < 0) return null;
    const n = viewNodes[popIdx];
    if (!n) return null;
    const rels = viewEdges
      .filter((e) => e.from === n.id || e.to === n.id)
      .slice(0, 6)
      .map((e) => {
        const other = viewNById[e.from === n.id ? e.to : e.from];
        return { rel: e.type.replace(/_/g, " "), name: other?.name ?? "?" };
      });
    const nMentions = story.mentions.filter((m) => m.entity === n.id).length;
    return { name: n.name, type: n.type, rels, nMentions };
  });

  // ── Plain (non-reactive) per-frame state ──
  let scrollerEl, stageEl, canvasEl, pageWrapEl;
  let heroCopyEl, captionEl, capNumEl, capTxtEl;
  let chunksWrapEl, chipsWrapEl, edgesSvgEl, popoutEl;
  let scalePanelEl, typeChipsEl, chatHeadEl, chatEl, groundedEl, ctasEl;
  let boxEls = [];
  let cardEls = [];
  let sentEls = [];
  const cardRest = []; // each card's resting stage rect (flight destinations)
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
  // mobile-only per-frame geometry: the centred page's rest centre and each
  // chip's half-width (to clamp chips fully inside the narrow viewport)
  let pageCX = 0;
  let pageCY = 0;
  let chipHalf = [];

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
    if (mobile) {
      // centre the narrower card on the tapped node, kept inside the viewport
      popoutEl.style.left = clamp(x - 130, 10, W - 270) + "px";
      popoutEl.style.top = clamp(y + 16, 12, H - 220) + "px";
    } else {
      popoutEl.style.left = clamp(x + 16, 12, W - 300) + "px";
      popoutEl.style.top = clamp(y + 14, 12, H - 240) + "px";
    }
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
    if (mobile) {
      // Portrait composition: as large as fits the width with a small margin,
      // and sat a little above centre so it tucks under the scene heading
      // instead of floating in the middle with dead bands top and bottom (the
      // "poor use of vertical height" on phones). A 0.77-aspect page can only
      // fill about half a tall phone, so the remaining space is pooled at the
      // bottom where the dot rail lives, not split awkwardly in two.
      pageW = Math.min(W * 0.78, H * 0.54 * aspect);
      pageH = pageW / aspect;
      const left = (W - pageW) / 2;
      const top = H * 0.46 - pageH / 2;
      pageWrapEl.style.left = left + "px";
      pageWrapEl.style.top = top + "px";
      pageCX = left + pageW / 2;
      pageCY = top + pageH / 2;
    } else {
      pageH = H * 0.76;
      pageW = pageH * aspect;
      if (pageW > W * 0.42) {
        pageW = W * 0.42;
        pageH = pageW / aspect;
      }
      pageWrapEl.style.left = W * 0.56 - pageW / 2 + "px";
      pageWrapEl.style.top = (H - pageH) / 2 + "px";
    }
    pageWrapEl.style.width = pageW + "px";
    pageWrapEl.style.height = pageH + "px";
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
    // Flight destinations: each card's resting rect. The container's own
    // translateY(-50%) shows up in its bounding rect; the cards' animated
    // translateX does NOT affect offsetLeft/offsetTop, so this is the rect a
    // card occupies once settled.
    const contRect = chunksWrapEl.getBoundingClientRect();
    cardEls.forEach((el, i) => {
      if (!el) return;
      cardRest[i] = {
        x: contRect.left + el.offsetLeft,
        y: contRect.top + el.offsetTop,
        w: el.offsetWidth,
        h: el.offsetHeight,
      };
    });
    chipEls.forEach((el, i) => {
      if (el) chipHalf[i] = el.offsetWidth / 2;
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
    // exclusion zone: keep nodes clear of the caption block, which sits
    // vertically centred in the left column during the entities scene
    if (sx < W * 0.38 && sy > H * 0.26 && sy < H * 0.78)
      sx = W * 0.38 + (W * 0.38 - sx) * 0.2;
    const kx = lerp(cx, W * 0.16, chatP);
    const ky = lerp(H * 0.78, H * 0.44, chatP);
    return [
      lerp(sx, kx + n.x * R * 0.12, shrink),
      lerp(sy, ky + n.y * R * 0.07, shrink),
    ];
  }

  // Portrait constellation: a narrow x radius keeps chips within the width
  // while a taller y radius spreads them down the screen. The caller clamps
  // each chip's centre by its measured half-width so no label is ever cut off.
  function nodePosMobile(n, shrink, chatP = 0) {
    const cx = W * 0.5;
    const cy = H * 0.52;
    const Rx = W * 0.4;
    const Ry = H * 0.32;
    let sx = cx + n.x * Rx;
    let sy = cy + n.y * Ry;
    if (sy < H * 0.2) sy = H * 0.2 + (H * 0.2 - sy) * 0.3;
    const kx = lerp(cx, W * 0.5, chatP);
    const ky = lerp(H * 0.72, H * 0.42, chatP);
    return [
      lerp(sx, kx + n.x * Rx * 0.2, shrink),
      lerp(sy, ky + n.y * Ry * 0.12, shrink),
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
    heroCopyEl.style.transform = mobile
      ? `translateX(-50%) translateY(${-pHero * 24}px)`
      : `translateY(${-pHero * 30}px)`;
    heroCopyEl.style.visibility = pHero >= 1 ? "hidden" : "visible";

    // page transform
    const settle = outCubic(sub(t, 0, 0.08));
    const slide = inOutCubic(sub(t, 0.26, 0.36));
    const recede = outCubic(sub(t, 0.44, 0.54));
    const gone = inOutCubic(sub(t, 0.66, 0.72));
    const rot = lerp(mobile ? -3.2 : -3.5, 0, settle);
    let px = 0;
    let sc;
    let pageTY = 0;
    if (mobile) {
      // page settles centred, then dissolves upward as the cards lift off
      // to the cards; during the hero it rides lower so the copy + cue clear it
      // (the offset is sized to clear the taller, higher resting page: at the
      // hero the whole scan sits below the SCROLL cue, then rises into place).
      const fade = inOutCubic(sub(t, 0.3, 0.44));
      sc = lerp(lerp(0.96, 1, settle), 0.88, slide);
      pageTY = (1 - settle) * H * 0.17 + lerp(0, -H * 0.06, slide);
      pageWrapEl.style.transform = `translate(0,${pageTY}px) rotate(${rot}deg) scale(${sc})`;
      pageWrapEl.style.opacity = 1 - fade;
    } else {
      px = lerp(lerp(0, -W * 0.34, slide), -W * 0.52, gone);
      sc = lerp(lerp(lerp(0.94, 1, settle), 0.78, slide), 0.66, recede);
      pageWrapEl.style.transform = `translate(${px}px,0) rotate(${rot}deg) scale(${sc})`;
      pageWrapEl.style.opacity = lerp(lerp(1, 0.25, recede), 0, gone);
    }

    // Flight timing first: each card's lift progress drives BOTH the card and
    // the disappearance of its own boxes, so a chunk's outlines vanish exactly
    // as ITS card lifts off (not on a global fade).
    const rawFly = sub(t, 0.27, 0.42);
    const flyP = inOutCubic(sub(t, 0.27, 0.38)); // global fallback (orphans)
    const cardFp = cards.map((_, i) =>
      inOutCubic(sub(rawFly, i * 0.07, i * 0.07 + 0.55)),
    );
    const pcx = mobile ? pageCX : W * 0.56 + px;
    const pcy = mobile ? pageCY + pageTY : H / 2;
    const pw = pageW * sc;
    const ph = pageH * sc;

    // layout boxes: crisp staggered scanner strokes, then each chunk's boxes
    // are taken by its lifting card
    const pLay = sub(t, 0.08, 0.2);
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
      if (rawFly <= 0) {
        el.style.opacity = dp;
        el.style.transform = `scaleY(${lerp(0.25, 1, dp)})`;
      } else {
        const ci = bboxCard[i];
        const lift = ci >= 0 ? cardFp[ci] : flyP;
        el.style.opacity = Math.max(0, 1 - lift * 3);
        el.style.transform = "none";
      }
    });

    // The cards THEMSELVES lift off the page: each card starts as its chunk's
    // footprint on the scan (union of its non-art boxes), flying text-aboard
    // to its resting slot. One object per chunk, no chip-to-card handoff.
    // Works on both compositions: pcx/pcy above are mobile-aware, and the
    // extraction exit keeps the mobile direction (up) vs desktop (right).
    const cardsOut = inOutCubic(sub(t, 0.5, 0.58));
    const outX = mobile ? 0 : cardsOut * 60;
    const outY = mobile ? -cardsOut * 26 : 0;
    cardEls.forEach((el, i) => {
      if (!el) return;
      const s = cardSrc[i];
      const d = cardRest[i];
      if (!s || !d) {
        const dp = outCubic(sub(rawFly, i * 0.07, i * 0.07 + 0.5));
        el.style.opacity = dp * (1 - cardsOut);
        el.style.transform = `translate(${outX}px,${outY}px)`;
        return;
      }
      const fp = cardFp[i];
      const srcX = pcx - pw / 2 + s.x * pw;
      const srcY = pcy - ph / 2 + s.y * ph;
      const srcW = (s.x2 - s.x) * pw;
      const scale = lerp(srcW / d.w, 1, fp);
      const tx = lerp(srcX, d.x, fp) - d.x + outX;
      const ty = lerp(srcY, d.y, fp) - d.y + outY;
      el.style.transformOrigin = "top left";
      el.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
      // fade in as it lifts, fade out toward the extraction handover
      el.style.opacity = Math.min(fp * 3, 1) * (1 - cardsOut);
    });
    chunksWrapEl.style.visibility = cardsOut >= 1 ? "hidden" : "visible";

    // Marks highlight like a hand sweeping a marker across each phrase: a
    // left-to-right fill wipe, staggered in DOM order (top-to-bottom, then
    // left-to-right down the cards). The whole sweep is compressed to finish
    // by ~0.515, just as the cards begin to lift off (cardsOut starts at 0.5),
    // so every phrase is fully marked before its card flies away. We only
    // animate paint properties (background + color): font-weight is NOT
    // touched, because changing weight reflows the glyphs and makes the text
    // visibly jump/grow as each word highlights.
    const HL_START = 0.44; // first phrase begins
    const HL_STAGGER = 0.045; // spread of start times across all marks
    const HL_FILL = 0.03; // each phrase's own (fast) swipe duration
    const nMark = markEls.length;
    markEls.forEach((m, i) => {
      const c = m.dataset.c;
      const frac = nMark > 1 ? i / (nMark - 1) : 0;
      const from = HL_START + frac * HL_STAGGER;
      const pen = sub(t, from, from + HL_FILL); // 0..1 swipe for this phrase
      if (pen <= 0) {
        m.style.background = "transparent";
        m.style.color = "inherit";
        return;
      }
      const pct = pen * 100;
      const tint = `color-mix(in srgb, ${c} 34%, transparent)`;
      // hard trailing edge with a small feather ahead of it = a marker tip
      // leading the fill from the left. The text stays ink (never recoloured to
      // the entity hue): a real highlighter is dark text on a colour fill, and
      // same-hue text on a same-hue tint is low-contrast and hard to read.
      m.style.background = `linear-gradient(90deg, ${tint} ${pct.toFixed(1)}%, transparent ${Math.min(100, pct + 5).toFixed(1)}%)`;
    });

    // chips fly from the card column to their graph nodes; edges draw in
    const pChip = sub(t, 0.47, 0.56);
    const shrink = inOutCubic(sub(t, 0.66, 0.74));
    const graphDim = outCubic(sub(t, 0.82, 0.88));
    const chatShift = inOutCubic(sub(t, 0.82, 0.9));
    viewNodes.forEach((n, i) => {
      const el = chipEls[i];
      if (!el) return;
      const raw = sub(pChip, i * 0.025, i * 0.025 + 0.4);
      const move = inOutCubic(raw);
      const pop = outBack(raw);
      let gx;
      let gy;
      let fromX;
      let fromY;
      let opa;
      let scl;
      if (mobile) {
        const [gx0, gy0] = nodePosMobile(n, shrink, chatShift);
        const hw = chipHalf[i] || 60;
        gx = clamp(gx0, hw + 10, W - hw - 10);
        gy = clamp(gy0, H * 0.12, H * 0.9);
        fromX = W * 0.5;
        fromY = H * 0.42;
        // during the shelf/chat phases the page's chips recede to a faint,
        // small knot so the constellation (the whole corpus) reads as the figure
        opa = Math.min(raw * 3, 1) * lerp(1, 0.22, Math.max(shrink, graphDim));
        scl = lerp(0.4, 1, pop) * lerp(1, 0.5, shrink);
      } else {
        [gx, gy] = nodePos(n, shrink, chatShift);
        fromX = W * 0.74;
        fromY = H * 0.5;
        opa =
          Math.min(raw * 3, 1) * lerp(1, 0.4, Math.max(shrink * 0.5, graphDim));
        scl = lerp(0.4, 1, pop) * lerp(1, 0.66, shrink);
      }
      lastNodePos[i] = [gx, gy];
      const x = lerp(fromX, gx, move);
      const y = lerp(fromY, gy, move);
      el.style.opacity = opa;
      el.style.transform = `translate(${x}px,${y}px) translate(-50%,-50%) scale(${scl})`;
    });
    placePopout();
    const pEdge = outCubic(sub(t, 0.52, 0.6));
    lineEls.forEach((l, i) => {
      if (!l) return;
      let x1;
      let y1;
      let x2;
      let y2;
      if (mobile) {
        // read the clamped chip centres so edges stay attached to the pills
        [x1, y1] = lastNodePos[viewIdxById[viewEdges[i].from]] || [0, 0];
        [x2, y2] = lastNodePos[viewIdxById[viewEdges[i].to]] || [0, 0];
      } else {
        [x1, y1] = nodePos(viewNById[viewEdges[i].from], shrink, chatShift);
        [x2, y2] = nodePos(viewNById[viewEdges[i].to], shrink, chatShift);
      }
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
          const edge = clamp(
            Math.min(x, W - x, y, H - y) / (mobile ? 70 : 90),
            0,
            1,
          );
          // keep the progress rail's lane clear (right gutter on desktop, the
          // bottom dot row on mobile)
          if (mobile) {
            if (y > H - 54) continue;
          } else if (x > W - 150 && y > H * 0.26 && y < H * 0.74) continue;
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

    // chat: headline fades in up top, panel rises to below it, and the answer
    // reveals sentence by sentence (staggered fades, fully scrub-reversible)
    const pChat = sub(t, 0.82, 1);
    const rise = outCubic(sub(pChat, 0, 0.14));
    chatHeadEl.style.opacity = rise * ease(sub(pChat, 0.04, 0.16));
    chatEl.style.transform = `translate(-50%, calc(-44% + ${lerp(120, 0, rise)}vh))`;
    const pSent = sub(pChat, 0.16, 0.66);
    const step = answerSents.length > 1 ? 0.72 / answerSents.length : 1;
    sentEls.forEach((el, j) => {
      if (!el) return;
      el.style.opacity = outCubic(sub(pSent, j * step, j * step + 0.28));
    });
    const pG = ease(sub(pChat, 0.7, 0.8));
    groundedEl.style.opacity = pG;
    groundedNodes.forEach((n) => {
      // grounded nodes come from the full layout; map by id to the current
      // view (a grounded node may sit at a different index, or be absent, in
      // the mobile subset).
      const i = viewIdxById[n.id];
      if (i == null || !chipEls[i]) return;
      chipEls[i].classList.toggle("lit", pG > 0.5);
      if (pG > 0.5) chipEls[i].style.opacity = 0.95;
    });
    ctasEl.style.opacity = ease(sub(pChat, 0.82, 0.92));

    // caption (left column, crossfades between phases). In the layout and
    // entities scenes the left column is free, so the caption sits vertically
    // centred beside the page, hero-style (.mid); the chunking scene keeps the
    // top-left slot because the page itself slides into the left column. The
    // caption is fully faded out at every phase boundary, so the position
    // switch is never visible.
    const cap = CAPTIONS[phase.id];
    if (cap) {
      const pin = sub(t, phase.start + 0.005, phase.start + 0.025);
      const pout =
        phase.id === "chat" ? 0 : sub(t, phase.end - 0.012, phase.end);
      captionEl.style.opacity = Math.min(pin, 1 - pout);
      captionEl.classList.toggle("mid", phase.id !== "chunks");
      capNumEl.textContent = cap[0];
      capTxtEl.textContent = cap[1];
    } else {
      captionEl.style.opacity = 0;
    }
  }

  // Past the hero the story is immersive: the app topbar fades out (the
  // layout styles .grimoire-app.story-immersed .topbar) and comes back as the
  // visitor reaches the finale, so they land back in normal chrome.
  let appEl = null;
  let immersed = false;
  function setImmersed(next) {
    if (next === immersed) return;
    immersed = next;
    appEl?.classList.toggle("story-immersed", next);
  }

  // Displayed timeline fraction, eased toward the scroll-derived target every
  // frame. Native scroll stays 1:1 with the document (scroll position, proximity
  // snap and keyboard are all untouched); only the ANIMATION interpolation is
  // smoothed. A mouse wheel moves scroll in big discrete notches, so without
  // this the choreography jumps a notch at a time; easing renders each notch as
  // a short glide. A trackpad already scrolls in small steps, so its already-
  // fluid feel is unchanged. (Joe-approved for the 2026-07 mobile-polish pass;
  // distinct from the rejected timed key-glides, which hijacked scroll itself.)
  let shownT = 0;
  const SCRUB_SMOOTH = 0.2; // per-frame approach fraction; higher = snappier
  function onFrame() {
    raf = 0;
    if (!measured) return; // wait for the first layout() before scrubbing
    const r = scrollerEl.getBoundingClientRect();
    const span = r.height - H;
    const target = span > 0 ? clamp(-r.top / span, 0, 1) : 0;
    lastT = target;
    const d = target - shownT;
    if (Math.abs(d) < 0.0004) {
      shownT = target;
    } else {
      shownT += d * SCRUB_SMOOTH;
      raf = requestAnimationFrame(onFrame); // keep gliding until settled
    }
    setImmersed(shownT > 0.03 && shownT < 0.97);
    frame(shownT);
  }

  onMount(() => {
    reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return; // stay on the static stacked scenes

    // Narrow/touch viewports get the portrait choreography; wider ones the
    // desktop two-column stage. Checked once here and re-checked on resize.
    mobile = window.matchMedia("(max-width: 700px)").matches;
    ready = true; // reveal the scrubbed stage (re-render, refs already bound)
    appEl = document.querySelector(".grimoire-app");
    // Proximity snap lives on the document scroll container: the .snap
    // markers' scroll-snap-align does nothing without it. html is outside this
    // component, so set it imperatively and remove it on destroy so other
    // routes are unaffected. NEVER "mandatory": free scrubbing is the point.
    // Desktop only: on a phone the snap fights touch-fling momentum and makes
    // the anchors feel like they catch and stutter, so mobile keeps pure native
    // momentum scrolling (applySnap tracks the mobile flag on resize too).
    const applySnap = () => {
      document.documentElement.style.scrollSnapType = mobile
        ? ""
        : "y proximity";
    };
    applySnap();
    // The story scrubs the DOCUMENT scroll, so body must be allowed to
    // overflow. Two stylesheets disagree about body overflow (design-system
    // says auto, the global reset says hidden) and which wins depends on
    // bundle order: prod happens to pick auto, vite dev picks hidden (frozen
    // page). Assert what we need instead of gambling on cascade order.
    document.body.style.overflow = "auto";
    buildDots();
    ctx = canvasEl.getContext("2d");
    refreshTypeColors();

    const queue = () => {
      if (!raf) raf = requestAnimationFrame(onFrame);
    };
    const onResize = () => {
      const nowMobile = window.matchMedia("(max-width: 700px)").matches;
      if (nowMobile !== mobile) mobile = nowMobile;
      applySnap();
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
      setImmersed(false);
      document.documentElement.style.scrollSnapType = "";
      document.body.style.overflow = "";
      window.removeEventListener("scroll", queue);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("click", onDocClick);
      window.removeEventListener("keydown", onKey);
      mo?.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  });
</script>

<!-- Preload the page scan as the LCP image. Under slow connections the scan was
     not painting before the static->scrubbed swap hid it, so Lighthouse recorded
     NO_LCP (which also voided TBT and the minify/unused diagnostics). Preloading
     with the same srcset/sizes as the <img> and high priority makes the bytes
     land early, so the LCP fires within the trace window. Pure loading hint, no
     effect on the choreography. -->
<svelte:head>
  <link
    rel="preload"
    as="image"
    href={story.image}
    imagesrcset={`${pageImageSm} 800w, ${story.image} 1200w`}
    imagesizes="(max-width: 700px) 88vw, 560px"
    fetchpriority="high"
  />
</svelte:head>

<!-- The extra `grimoire` class scopes the --grim-* theme tokens onto the story
     subtree, so every surface here (and the canvas color resolver, which reads
     computed styles off this element) resolves the same palette the rest of the
     app uses. -->
<div class="scrollstory grimoire" class:ready class:mobile>
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
            >Who is the Black Spider?</span
          >
        </h1>
        <p>Scroll to watch Grimoire find the answer.</p>
        <div class="cue">SCROLL <span class="arrow">&darr;</span></div>
      </div>

      <div class="page-wrap" bind:this={pageWrapEl}>
        <img
          src={story.image}
          srcset={`${pageImageSm} 800w, ${story.image} 1200w`}
          sizes="(max-width: 700px) 88vw, 560px"
          width="1200"
          height="1553"
          fetchpriority="high"
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
          {#each viewEdges as e, i (i)}
            <line bind:this={lineEls[i]} />
          {/each}
        </svg>
        <div class="chips" bind:this={chipsWrapEl}>
          {#each viewNodes as n, i (n.id)}
            <button
              class="chip"
              class:open={popIdx === i}
              bind:this={chipEls[i]}
              onclick={(ev) => {
                ev.stopPropagation();
                togglePopout(i);
              }}
            >
              <span class="dot" style="background:{typeVar(n.type)}"
              ></span>{n.name}
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
            style="color:{typeVar(
              popData.type,
            )};background:color-mix(in srgb, {typeVar(
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
            found in {popData.nMentions} mention{popData.nMentions === 1
              ? ""
              : "s"} on this page
          </div>
        {/if}
      </div>

      <div class="scale-panel" bind:this={scalePanelEl}>
        <div class="scale-head grim-title section-head">
          Unearthed from this page
        </div>
        <div class="this-page">
          <span>{story.bboxes.length} text blocks</span><span class="k">/</span>
          <span>{story.chunks.length} passages</span><span class="k">/</span>
          <span>{story.entities.length} characters, places and items</span><span
            class="k">/</span
          >
          <span>{graphEdges.length} relationships</span>
        </div>
        <div class="scale-head grim-title section-head compendium-head">
          The entire compendium contains
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
          Every claim links back to the passage it came from. Click one.
        </div>
      </div>

      <div class="chat" bind:this={chatEl}>
        {#if isPlaceholder}
          <div class="mock-note">example conversation</div>
        {/if}
        <div class="bubble-q">{transcript.question}</div>
        <div class="bubble-a">
          {#each answerSents as s, j (j)}
            {#if j > 0 && s.pi !== answerSents[j - 1].pi}
              <span class="pbreak"></span>
            {/if}
            <span class="sent" bind:this={sentEls[j]}>{@html s.html}</span>
            {" "}
          {/each}
        </div>
        <div class="grounded" bind:this={groundedEl}>
          <span class="lbl">GROUNDED IN</span>
          {#each groundedNodes as n (n.id)}
            <button
              class="gchip"
              onclick={(ev) => {
                ev.stopPropagation();
                const r = ev.currentTarget.getBoundingClientRect();
                togglePopout(viewIdxById[n.id] ?? -1, [r.left, r.top - 250]);
              }}
            >
              <span class="dot" style="background:{typeVar(n.type)}"
              ></span>{n.name}
            </button>
          {/each}
        </div>
        <div class="ctas" bind:this={ctasEl}>
          <a class="cta primary" href={chatHref()}>Ask the Grimoire</a>
          <a class="cta ghost" href={worldHref()}>Explore the world</a>
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
      <!-- The visible .lbl is display:none on mobile, and display:none text is
           dropped from the a11y tree, so the dot-only button would be nameless
           there. aria-label names it on every form factor. -->
      <button
        class:on={phaseId === p.id}
        aria-label={p.label}
        aria-current={phaseId === p.id ? "true" : undefined}
        onclick={() => railTo(p)}
      >
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
          >Who is the Black Spider?</span
        >
      </h1>
      <p>
        The answer is buried in this page of Lost Mine of Phandelver. Grimoire
        digs it out below.
      </p>
    </section>

    <section class="static-scene">
      <p class="static-cap">
        <b>1 / Reading the page.</b> The scan gets picked apart into headers, columns,
        asides and art.
      </p>
      <div class="static-page" style="aspect-ratio:{aspect}">
        <img
          src={story.image}
          srcset={`${pageImageSm} 800w, ${story.image} 1200w`}
          sizes="(max-width: 700px) 88vw, 560px"
          width="1200"
          height="1553"
          fetchpriority="high"
          alt="Scanned book page: Nezznar the Black Spider"
        />
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
      <p class="static-cap">
        <b>2 / Broken into passages.</b> The page becomes readable passages in the
        right order, each tagged with the section it came from.
      </p>
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
      <p class="static-cap">
        <b>3 / Who and what is on the page.</b> Every character, place and item gets
        picked out, along with how they connect.
      </p>
      <svg
        class="static-graph"
        viewBox="0 0 100 100"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Relationship graph of the entities on this page"
      >
        {#each graphEdges as e, i (i)}
          <line
            x1={50 + nById[e.from].x * 40}
            y1={50 + nById[e.from].y * 32}
            x2={50 + nById[e.to].x * 40}
            y2={50 + nById[e.to].y * 32}
          />
        {/each}
        {#each nodes as n (n.id)}
          <circle
            cx={50 + n.x * 40}
            cy={50 + n.y * 32}
            r="1.4"
            fill={typeVar(n.type)}
          />
        {/each}
      </svg>
      <ul class="static-roster">
        {#each nodes as n (n.id)}
          <li>
            <span class="dot" style="background:{typeVar(n.type)}"
            ></span>{n.name}
          </li>
        {/each}
      </ul>
    </section>

    <section class="static-scene">
      <p class="static-cap">
        <b>4 / Every book.</b>
      </p>
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
      <p class="static-cap">
        <b>5 / Grounded answers.</b> Every claim links back to the passage it came
        from. Click one.
      </p>
      <div class="chat static-chat">
        {#if isPlaceholder}
          <div class="mock-note">example conversation</div>
        {/if}
        <div class="bubble-q">{transcript.question}</div>
        <div class="bubble-a">
          {#each answerSents as s, j (j)}
            {#if j > 0 && s.pi !== answerSents[j - 1].pi}
              <span class="pbreak"></span>
            {/if}
            <span class="sent">{@html s.html}</span>
            {" "}
          {/each}
        </div>
        <div class="grounded static-grounded">
          <span class="lbl">GROUNDED IN</span>
          {#each groundedNodes as n (n.id)}
            <span class="gchip">
              <span class="dot" style="background:{typeVar(n.type)}"
              ></span>{n.name}
            </span>
          {/each}
        </div>
        <div class="ctas static-ctas">
          <a class="cta primary" href={chatHref()}>Ask the Grimoire</a>
          <a class="cta ghost" href={worldHref()}>Explore the world</a>
          <a class="cta ghost" href={libraryHref()}>Browse the library</a>
        </div>
      </div>
    </section>
  </div>
</div>

<style>
  /* All colors are --grim-* tokens; the scoped .grimoire ancestor (the app
     shell) resolves them. */

  /* Never let a scene element widen the document on a phone. `clip` (not
     `hidden`) does not create a scroll container, so the sticky .stage inside
     still sticks to the viewport; it just trims any horizontal spill. */
  .scrollstory {
    overflow-x: clip;
  }

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
    /* dvh tracks the *visible* viewport as the mobile URL bar shows/hides, so
       the sticky stage always matches what the per-frame geometry measures with
       window.innerHeight. With plain vh the stage stayed large-viewport tall
       while innerHeight shrank, so the bottom of each scene fell off screen as
       the bar animated (the "janky, scrolls things off" feel on phones). */
    height: 100dvh;
    overflow: hidden;
    background: var(--grim-paper);
  }

  /* hero copy */
  .hero-copy {
    position: absolute;
    left: 6vw;
    top: 24vh;
    /* use the whitespace: the page lane starts at ~35vw */
    max-width: 28vw;
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
    font-size: 16px;
    line-height: 1.6;
    margin-top: 16px;
  }
  .cue {
    margin-top: 26px;
    color: var(--grim-ink-soft);
    font-size: 12.5px;
    font-weight: 600;
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

  /* caption: left column, clear of the page/cards/chat lanes. Sized as the
     co-star of each scene (hero-like hierarchy), not a corner footnote. */
  .caption {
    position: absolute;
    left: 6vw;
    top: 6vh;
    z-index: 8;
    max-width: 32ch;
    opacity: 0;
    pointer-events: none;
    padding: 12px 16px 14px;
    margin: -12px -16px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--grim-paper) 80%, transparent);
    backdrop-filter: blur(3px);
  }
  /* layout + entities scenes: the left column is free, so the caption sits
     vertically centred beside the page, mirroring the hero composition. The
     chunking scene keeps the top-left slot (the page slides under it).
     frame() toggles .mid; desktop only, mobile keeps its top band. */
  .scrollstory:not(.mobile) .caption.mid {
    top: 50%;
    transform: translateY(-50%);
  }
  .caption .num {
    font-size: 12.5px;
    color: var(--grim-accent);
    font-weight: 700;
    letter-spacing: 0.16em;
    font-family: var(--font-mono);
  }
  .caption .txt {
    font-family: var(--grim-serif);
    font-size: clamp(18px, 2vw, 25px);
    line-height: 1.4;
    margin-top: 8px;
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
  }
  .bbox.k-header {
    background: color-mix(in srgb, var(--grim-type-creature) 14%, transparent);
  }
  .bbox.k-text,
  .bbox.k-caption {
    border-color: var(--grim-type-spell);
  }
  .bbox.k-text,
  .bbox.k-caption {
    background: color-mix(in srgb, var(--grim-type-spell) 10%, transparent);
  }
  .bbox.k-aside {
    border-color: var(--grim-type-npc);
  }
  .bbox.k-aside {
    background: color-mix(in srgb, var(--grim-type-npc) 14%, transparent);
  }
  .bbox.k-art {
    border-color: var(--grim-type-faction);
    background: color-mix(in srgb, var(--grim-type-faction) 10%, transparent);
  }

  .attribution {
    position: absolute;
    left: 2px;
    bottom: -26px;
    font-size: 11px;
    color: var(--grim-ink-soft);
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
    box-shadow: 0 0 0 3px
      color-mix(in srgb, var(--grim-accent) 45%, transparent);
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
    background: color-mix(in srgb, var(--grim-paper) 94%, transparent);
    backdrop-filter: blur(8px);
    border: 1px solid var(--grim-line);
    box-shadow: 0 12px 44px rgba(10, 14, 22, 0.16);
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
  /* The two panel hooks ("Unearthed from this page" / "The entire compendium
     contains") are one matched pair: a repeated [heading -> counts] rhythm.
     Sized below the 40px hero scale so both headings can breathe and the big
     corpus numbers stay the loudest element. */
  .section-head {
    font-size: clamp(21px, 2.6vw, 32px);
  }
  .compendium-head {
    margin-top: 34px;
  }
  .this-page {
    margin-top: 10px;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--grim-ink);
    display: flex;
    justify-content: center;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    font-family: var(--font-mono);
  }
  .this-page .k {
    color: var(--grim-text-dim);
    font-weight: 700;
    font-size: 12px;
  }
  .counters {
    display: flex;
    justify-content: center;
    gap: clamp(18px, 4.5vw, 58px);
    margin-top: 14px;
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
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--grim-ink);
    margin-top: 6px;
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
    padding: 6px 14px 6px 9px;
    font-size: 14px;
    font-weight: 600;
    box-shadow: 0 2px 8px rgba(10, 14, 22, 0.12);
    color: var(--grim-ink);
    font-family: var(--font-mono);
  }
  .tchip .dot {
    width: 10px;
    height: 10px;
  }
  .tchip .n {
    color: var(--grim-ink);
    font-variant-numeric: tabular-nums;
    font-weight: 700;
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
  /* Accent-tinted rather than accent-filled: white-on-blue at this size read
     poorly (Joe, 2026-07 UX pass), so the bubble keeps its "user message"
     shape while the text stays full-contrast ink on a light tint. */
  .bubble-q {
    background: var(--grim-accent-soft);
    color: var(--grim-ink);
    border: 1px solid color-mix(in srgb, var(--grim-accent) 35%, transparent);
    border-radius: 12px 12px 3px 12px;
    padding: 9px 13px;
    font-size: 14px;
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
  /* sentence-level reveal: the scrubbed scene drives .sent opacity per frame
     (plain inline spans so text flow and wrapping stay natural); the static
     scene leaves them at full opacity. .pbreak renders the paragraph gap. */
  .scroller .sent {
    opacity: 0;
  }
  .bubble-a .pbreak {
    display: block;
    height: 0.8em;
  }
  .bubble-a :global(mark) {
    background: transparent;
    font-weight: 600;
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
    font-size: 11.5px;
    letter-spacing: 0.18em;
    color: var(--grim-text-dim);
    font-weight: 700;
    font-family: var(--font-mono);
  }
  .gchip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 3px 11px 3px 7px;
    font-size: 12px;
    font-weight: 600;
    background: var(--grim-surface-2);
    font-family: var(--font-mono);
    color: var(--grim-ink);
    cursor: pointer;
  }
  .gchip .dot {
    width: 8px;
    height: 8px;
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
    padding: 12px 10px;
    border-radius: 7px;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    text-decoration: none;
    font-family: var(--font-mono);
  }
  /* No accent-filled buttons: light-on-blue mono caps read poorly at button
     sizes (Joe, 2026-07 UX pass, round 2). The primary keeps top billing via
     a stronger tint + solid accent border; ghosts stay lighter-bordered. */
  .cta.primary {
    background: color-mix(in srgb, var(--grim-accent) 12%, var(--grim-surface));
    color: var(--grim-accent);
    border: 1.5px solid var(--grim-accent);
  }
  .cta.ghost {
    border: 1.5px solid color-mix(in srgb, var(--grim-accent) 45%, transparent);
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

  /* ── dedicated mobile (portrait) composition of the scrubbed stage ──
     Applied only when `mobile` is set (below 700px). The frame() geometry
     switches to a single centred column; these rules restyle the same DOM to
     match. Ported from reference-mockup-mobile.html: keep the two in sync. */
  .scrollstory.mobile .hero-copy {
    left: 50%;
    top: 12vh;
    width: 86vw;
    max-width: 34ch;
    text-align: center;
    /* frame() writes `translateX(-50%) translateY(...)` here every frame */
  }
  .scrollstory.mobile .hero-copy p {
    margin-left: auto;
    margin-right: auto;
  }
  .scrollstory.mobile .caption {
    left: 5vw;
    top: 4vh;
    max-width: 88vw;
  }
  .scrollstory.mobile .caption .txt {
    font-size: 16px;
    max-width: 30ch;
  }
  .scrollstory.mobile .attribution {
    left: 0;
    right: 0;
    bottom: -34px;
    text-align: center;
    white-space: normal;
    font-size: 9.5px;
    line-height: 1.4;
  }
  .scrollstory.mobile .chunks {
    left: 4vw;
    right: 4vw;
    top: 15vh;
    width: auto;
    transform: none;
    gap: 7px;
  }
  .scrollstory.mobile .chunk-card {
    padding: 8px 12px;
  }
  .scrollstory.mobile .chunk-card .body {
    font-size: 12px;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }
  .scrollstory.mobile .chip {
    font-size: 10px;
    padding: 3px 9px 3px 6px;
  }
  .scrollstory.mobile .popout {
    width: 260px;
  }
  .scrollstory.mobile .scale-panel,
  .scrollstory.mobile .chat-head {
    top: 8vh;
    width: 92vw;
    padding: 18px;
    border-radius: 16px;
  }
  .scrollstory.mobile .chat-head {
    top: 7vh;
  }
  .scrollstory.mobile .this-page {
    gap: 7px;
    font-size: 12px;
  }
  .scrollstory.mobile .counters {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px 8px;
  }
  .scrollstory.mobile .type-chips {
    max-width: 92vw;
    gap: 6px;
  }
  .scrollstory.mobile .chat {
    width: 92vw;
    padding: 18px 18px 16px;
  }
  .scrollstory.mobile .ctas {
    flex-direction: column;
    gap: 8px;
  }
  .scrollstory.mobile .cta {
    padding: 12px 8px;
  }
  /* progress rail: a horizontal dot row pinned to the bottom, out of the
     content's way (the desktop right gutter has no room on a phone) */
  .scrollstory.mobile .rail {
    right: auto;
    left: 50%;
    top: auto;
    bottom: 12px;
    transform: translateX(-50%);
    flex-direction: row;
    gap: 12px;
    padding: 8px 14px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--grim-paper) 78%, transparent);
    backdrop-filter: blur(4px);
    box-shadow: 0 2px 10px rgba(10, 14, 22, 0.1);
  }
  .scrollstory.mobile .rail .lbl {
    display: none;
  }
  /* The visible dot stays 7px, but the tap target grows to a comfortable 28px
     square (over the 24px min) so the dots are not a fiddly hit on a phone. */
  .scrollstory.mobile .rail button {
    min-width: 28px;
    min-height: 28px;
    justify-content: center;
    gap: 0;
  }

  /* ── responsive ── */
  @media (max-width: 700px) {
    .static-cards {
      grid-template-columns: 1fr;
    }
  }
</style>
