<script>
  // "Boot once, restore forever": the public /ember/firecracker scroll-scrubbed
  // explainer of the fc-invoke daemon. A tall scroller wraps a
  // position: sticky full-viewport stage; one rAF loop maps scroll progress
  // to a master fraction t in [0, 1], and the pure timeline.js module turns
  // t into per-element geometry that we apply imperatively to plain
  // (non-reactive) element refs. Same ExploreCanvas / grimoire ScrollStory
  // discipline: 60fps writes never go through Svelte's reactivity proxy.
  //
  // Faithful port of the approved reference mockup (./reference-mockup.html).
  // Palette hex values live in ./fcstory.css as CSS custom properties (this
  // file's <style> block only ever references var(--fc-*)).
  import { onMount } from "svelte";
  import {
    PHASES,
    clamp,
    lerp,
    sub,
    easeInOut,
    easeOut,
    coldSegments,
    restoreBarFraction,
    captionOpacity,
  } from "./timeline.js";
  import { cold, restores } from "./data/trace.js";
  import { sandboxRestoreMs } from "./metrics.js";
  import ReplayWidget from "./ReplayWidget.svelte";
  import "./fcstory.css";

  const PHASE_HUMAN = {
    auth_tokenreview: "verify the caller",
    acquire_slot: "take a slot",
    snapshot_restore: "disk → memory",
    guest_wait_ready: "guest ready",
    guest_exec: "run the code",
  };

  const coldBoot = cold.phases.find((p) => p.name === "firecracker_boot");
  const coldWait = cold.phases.find((p) => p.name === "guest_wait_ready");
  const coldSave = cold.phases.find((p) => p.name === "snapshot_save");
  const segs = coldSegments(cold);
  const segByName = Object.fromEntries(segs.map((s) => [s.name, s]));

  const firstRestore = restores[0];
  const restoreTotalMin = Math.min(...restores.map((r) => r.total));
  const restoreTotalMax = Math.max(...restores.map((r) => r.total));
  // "Every sandbox since" is a claim about all restores, not just run #1, so
  // the hero and static fallback use the mean snapshot_restore duration
  // across every baked run (22.46 ms over the 12 runs, rendered as 22 ms;
  // the mockup's "23 milliseconds" was the rounded 180-run trace average).
  // Derived once in metrics.js, the single source of truth for this figure
  // across the whole public site.
  const meanSnapshotRestoreMs = sandboxRestoreMs;

  // ── Reactive state (coarse only; the per-frame path never touches these) ──
  // Motion preference is read once in onMount; display of the scrubbed vs
  // static stage is driven entirely by the prefers-reduced-motion CSS media
  // query, so there is no reactive `ready` flag flipping layout after paint.
  let reduced = false;

  // ── Plain (non-reactive) per-frame refs ──
  let scrollerEl, stageEl, machineEl, cellsEl, ramGlowEl;
  let heroEl, topbarEl, brandEl, crumbEl;
  let capBootEl, capFreezeEl, capRestoreEl, capRepeatEl;
  let vmEl, vmstateEl, diskEl, fileMemEl, fileVmsEl, fillMemEl, fillVmsEl;
  let trackEl, coldBarEl, coldTotalEl, restoreRowEl, restoreBarEl, zoomEl;
  let chipsEl;
  let chipEls = [];
  let dotEls = [];

  let vh = 0;
  let span = 1;
  let machineW = 0;
  let machineH = 0;
  let cells = [];

  function buildCells() {
    // Grid layout divides the panel exactly; each cell gets a stable
    // threshold = column position + jitter, so the sweep tracks time but the
    // edge is ragged. Math.random() only ever runs here, from onMount, never
    // at module/SSR eval time.
    if (!cellsEl) return;
    const rw = cellsEl.clientWidth;
    const rh = cellsEl.clientHeight;
    const ncols = Math.max(10, Math.round(rw / 34));
    const nrows = Math.max(6, Math.round(rh / 30));
    cellsEl.style.gridTemplateColumns = `repeat(${ncols},1fr)`;
    cellsEl.style.gridTemplateRows = `repeat(${nrows},1fr)`;
    cellsEl.innerHTML = "";
    cells = [];
    for (let i = 0; i < ncols * nrows; i++) {
      const col = i % ncols;
      const d = document.createElement("div");
      d.className = "cell";
      const hotHue = 16 + Math.random() * 9;
      const hotSat = 72 + Math.random() * 12;
      const hotLight = 46 + Math.random() * 14;
      const coldHue = 208 + Math.random() * 12;
      const coldSat = 46 + Math.random() * 14;
      const coldLight = 64 + Math.random() * 12;
      d.style.setProperty("--hot", `hsl(${hotHue} ${hotSat}% ${hotLight}%)`);
      d.style.setProperty(
        "--cold",
        `hsl(${coldHue} ${coldSat}% ${coldLight}%)`,
      );
      cellsEl.appendChild(d);
      cells.push({
        el: d,
        th: (col + 0.2 + Math.random() * 2.6) / (ncols + 2.6),
        state: "",
      });
    }
  }

  function setCells(get) {
    for (const c of cells) {
      const s = get(c);
      if (s !== c.state) {
        c.state = s;
        c.el.className = "cell" + (s ? " " + s : "");
      }
    }
  }

  let lastCellW = 0;
  function measure() {
    vh = window.innerHeight;
    span = scrollerEl.offsetHeight - vh;
    machineW = machineEl.offsetWidth;
    machineH = machineEl.offsetHeight;
    // buildCells() clears and rebuilds the whole memory-cell grid via
    // innerHTML, which is expensive. On mobile the URL bar showing/hiding
    // fires `resize` constantly *during* scroll, so rebuild only when the
    // grid's width actually changed; a height-only change (the URL-bar case)
    // leaves the 1fr rows to stretch and skips the churn that caused jank.
    const cw = cellsEl ? cellsEl.clientWidth : 0;
    if (cw !== lastCellW) {
      lastCellW = cw;
      buildCells();
    }
  }

  function cap(elm, t, a, b) {
    const { opacity, entrance } = captionOpacity(t, a, b);
    elm.style.opacity = opacity;
    // Write ONLY the dynamic entrance offset as a custom property; the base
    // transform (translateY(-50%) when the caption is vertically centered on
    // desktop, vs 0 for the mobile bottom-sheet) is owned by CSS so the media
    // query can differ. Writing element.style.transform directly here would be
    // an inline style that overrides the mobile `transform: none` rule and lift
    // the bottom sheet up over the timing track.
    elm.style.setProperty("--cap-enter", `${(1 - entrance) * 24}px`);
  }

  function frame(t) {
    // Fade the branding out the moment the reader scrolls off the hero, and
    // keep it gone for the rest of the story and the replay section (it used
    // to only dim to 0.3, then snap back to full opacity near the end and
    // collide with the "Feel it. Restore one now." heading).
    const topbarO = 1 - sub(t, 0.02, 0.06);
    topbarEl.style.opacity = topbarO;
    // The home and /ember crumb links are the only interactive things in the
    // (pointer-events:none) topbar; disable them once the bar has faded so
    // they are never invisible click targets over the story.
    const linkPE = topbarO > 0.5 ? "auto" : "none";
    brandEl.style.pointerEvents = linkPE;
    crumbEl.style.pointerEvents = linkPE;

    const ho = 1 - sub(t, PHASES.heroOut[0], PHASES.heroOut[1]);
    heroEl.style.opacity = ho;
    heroEl.style.transform = `translateY(${-(1 - ho) * 8}vh)`;
    heroEl.style.pointerEvents = ho > 0.5 ? "auto" : "none";

    /* ---- build ---- */
    const b = sub(t, PHASES.build[0], PHASES.build[1]);
    vmEl.style.opacity = sub(b, 0, 0.15);
    vmEl.style.transform = `translateY(${(1 - easeOut(sub(b, 0, 0.2))) * 30}px)`;
    const ignite = easeInOut(sub(b, 0.12, 0.82));
    ramGlowEl.style.opacity = ignite * 0.9 * (t < PHASES.freeze[0] ? 1 : 0);
    trackEl.style.opacity = sub(b, 0.05, 0.2);
    const coldReveal = easeInOut(sub(b, 0.1, 1));
    coldBarEl.style.clipPath = `inset(0 ${(1 - coldReveal) * 100}% 0 0)`;
    coldTotalEl.textContent =
      Math.round(coldReveal * cold.total).toLocaleString() + " ms";
    if (t < PHASES.freeze[0]) {
      vmstateEl.textContent =
        b < 0.12 ? "booting" : b < 0.85 ? "warming up" : "ready";
      vmstateEl.className = "state run";
    }

    /* ---- freeze ---- */
    const f = sub(t, PHASES.freeze[0], PHASES.freeze[1]);
    diskEl.style.opacity = sub(f, 0, 0.18);
    diskEl.style.transform = `translateY(${(1 - easeOut(sub(f, 0, 0.25))) * 16}px)`;
    const frostReveal = easeInOut(sub(f, 0.15, 0.85));
    if (t >= PHASES.freeze[0] && t < PHASES.restore[0]) {
      ramGlowEl.style.opacity = (1 - frostReveal) * 0.5;
      vmstateEl.textContent = frostReveal > 0.9 ? "frozen" : "freezing";
      vmstateEl.className = "state " + (frostReveal > 0.9 ? "frozen" : "run");
    }
    [fileMemEl, fileVmsEl].forEach((fe, i) => {
      const fo = sub(f, 0.25 + i * 0.12, 0.5 + i * 0.12);
      fe.style.opacity = fo;
      fe.style.transform = `translateY(${(1 - easeOut(fo)) * -14}px)`;
    });
    fillMemEl.style.transform = `scaleX(${easeInOut(sub(f, 0.3, 0.9))})`;
    fillVmsEl.style.transform = `scaleX(${easeInOut(sub(f, 0.45, 0.95))})`;

    /* ---- restore ---- */
    const r = sub(t, PHASES.restore[0], PHASES.restore[1]);
    if (t >= PHASES.restore[0]) {
      const reheat = easeInOut(sub(r, 0.1, 0.55));
      ramGlowEl.style.opacity = reheat * 0.9;
      vmstateEl.textContent =
        reheat > 0.95 ? "resumed" : reheat > 0.02 ? "restoring" : "frozen";
      vmstateEl.className = "state " + (reheat > 0.02 ? "run" : "frozen");
    }
    restoreRowEl.style.opacity = sub(r, 0.15, 0.3);
    restoreBarEl.style.clipPath = `inset(0 ${(1 - easeInOut(sub(r, 0.25, 0.45))) * 100}% 0 0)`;
    zoomEl.style.opacity = sub(r, 0.5, 0.68);
    zoomEl.style.transform = `translateY(${(1 - easeOut(sub(r, 0.5, 0.7))) * 10}px)`;

    /* ---- particles ---- */
    const yTop = 0.22;
    const yDisk = 0.78;
    const nDots = dotEls.length;
    dotEls.forEach((d, i) => {
      const off = i / nDots;
      let p = 0;
      let dir = 0;
      if (t >= PHASES.freeze[0] && t < PHASES.freeze[1]) {
        p = sub(f, 0.15 + off * 0.35, 0.5 + off * 0.35);
        dir = 1;
      } else if (t >= PHASES.restore[0] && t < PHASES.restore[1]) {
        p = sub(r, 0.05 + off * 0.3, 0.35 + off * 0.3);
        dir = -1;
      }
      if (dir === 0 || p <= 0 || p >= 1) {
        d.style.opacity = 0;
        return;
      }
      const y =
        dir === 1
          ? lerp(yTop, yDisk, easeOut(p))
          : lerp(yDisk, yTop, easeOut(p));
      const x =
        0.08 +
        ((i * 37) % 84) / 100 +
        Math.sin(p * Math.PI) * 0.02 * (i % 2 ? 1 : -1);
      d.style.transform = `translate(${x * machineW}px,${y * machineH}px)`;
      d.style.opacity = Math.sin(p * Math.PI);
      d.className = "page-dot" + (dir === -1 ? " hot" : "");
    });

    /* ---- repeat: chips ---- */
    const rp = sub(t, PHASES.repeat[0], PHASES.repeat[1]);
    const showChips = t >= PHASES.repeat[0] - 0.01;
    chipsEl.style.opacity = showChips ? 1 : 0;
    machineEl.style.opacity = showChips ? 1 - sub(rp, 0, 0.15) : 1;
    trackEl.style.opacity = showChips
      ? 1 - sub(rp, 0, 0.15)
      : trackEl.style.opacity;
    zoomEl.style.opacity = showChips ? 0 : zoomEl.style.opacity;
    chipEls.forEach((c, i) => {
      const co = sub(rp, 0.08 + i * 0.055, 0.2 + i * 0.055);
      c.style.opacity = co;
      c.style.transform = `scale(${lerp(0.86, 1, easeOut(co))})`;
    });
    const out = sub(t, PHASES.out[0], PHASES.out[1]);
    chipsEl.style.opacity = showChips ? 1 - out : 0;

    /* memory cells: state from the master timeline; setCells only touches
       cells whose state changed, the CSS transition does the actual fade */
    const reheatS = easeInOut(sub(r, 0.1, 0.55));
    let get;
    if (t < PHASES.build[0]) get = () => "";
    else if (t < PHASES.freeze[0]) get = (c) => (ignite >= c.th ? "hot" : "");
    else if (t < PHASES.restore[0])
      get = (c) => (frostReveal >= c.th ? "cold" : "hot");
    else get = (c) => (reheatS >= c.th ? "hot" : "cold");
    setCells(get);

    /* captions */
    cap(capBootEl, t, PHASES.build[0] + 0.01, PHASES.freeze[0] + 0.02);
    cap(capFreezeEl, t, PHASES.freeze[0] + 0.04, PHASES.restore[0] + 0.02);
    cap(capRestoreEl, t, PHASES.restore[0] + 0.04, PHASES.repeat[0] + 0.01);
    cap(capRepeatEl, t, PHASES.repeat[0] + 0.03, PHASES.out[1] - 0.01);
  }

  let ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      frame(clamp(window.scrollY / span, 0, 1));
      ticking = false;
    });
  }

  let resizing = false;
  function onResize() {
    if (resizing) return;
    resizing = true;
    requestAnimationFrame(() => {
      measure();
      frame(clamp(window.scrollY / span, 0, 1));
      resizing = false;
    });
  }

  onMount(() => {
    reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return; // CSS media query already shows the static scenes

    // The scrubbed stage is the SSR default now (see the .scroller /
    // .static-story rules), so there is no display swap on mount and thus no
    // layout shift: we only wire up interactivity here.
    //
    // No scroll-snap. The story has zero scroll-snap-align targets, so the
    // old `scrollSnapType = "y proximity"` on <html> snapped nothing on
    // purpose yet still grabbed momentum scrolling on mobile ("locks in
    // place"). Free scrubbing is the whole point, so it is simply gone.
    //
    // The story scrubs the DOCUMENT scroll, so body must be allowed to
    // overflow. Two stylesheets disagree about body overflow and which wins
    // depends on bundle order, so assert it instead of gambling on cascade.
    document.body.style.overflow = "auto";

    window.addEventListener("resize", onResize, { passive: true });
    window.addEventListener("scroll", onScroll, { passive: true });
    // measure() reads scrollerEl.offsetHeight; defer one frame so the initial
    // layout is painted and span is never captured as 0 (same guard grimoire's
    // ScrollStory.svelte uses).
    requestAnimationFrame(() => {
      measure();
      frame(0);
    });

    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onScroll);
    };
  });
</script>

<div class="fcstory">
  <header class="topbar" bind:this={topbarEl}>
    <span
      ><a class="brand" href="/" bind:this={brandEl}
        ><strong>jomcgi.dev</strong></a
      >
      / <a class="brand" href="/ember" bind:this={crumbEl}>ember</a> / firecracker</span
    >
  </header>

  <!-- ==================== SCROLL STORY ==================== -->
  <div class="scroller" bind:this={scrollerEl}>
    <div class="stage" bind:this={stageEl}>
      <div class="frame">
        <!-- hero -->
        <div class="hero" bind:this={heroEl}>
          <h1>
            Boot <span class="cold-word">once</span>.<br />Restore
            <span class="warm-word">forever</span>.
          </h1>
          <p>
            This site runs untrusted code in Firecracker microVMs. Building the
            first one took
            <span class="num big frost-text"
              >{(cold.total / 1000).toFixed(1)} seconds</span
            >. Every sandbox since has woken from disk in about
            <span class="num ember-text"
              >{Math.round(meanSnapshotRestoreMs)} milliseconds</span
            >. Scroll to see how.
          </p>
          <div class="cue">scroll &#9660;</div>
        </div>

        <!-- captions -->
        <div class="caption" bind:this={capBootEl}>
          <h2>
            Firecracker boots a fresh microVM in <em class="num"
              >{coldBoot.ms.toFixed(0)}&nbsp;ms</em
            >.
          </h2>
          <p>
            The slow part is waiting for a useful guest: kernel up, agent
            listening, toolchain warm. Measured cold, that wait is
            <span class="num big">{coldWait.ms.toLocaleString()}&nbsp;ms</span>.
          </p>
        </div>
        <div class="caption" bind:this={capFreezeEl}>
          <h2>
            So the daemon builds it once, then
            <span class="cool">freezes it</span>.
          </h2>
          <p>
            Every page of guest RAM and every device register is written to
            disk: a memory file and a vmstate file. Saving the snapshot takes
            <span class="num big">{coldSave.ms.toLocaleString()}&nbsp;ms</span>,
            paid one time at startup.
          </p>
        </div>
        <div class="caption" bind:this={capRestoreEl}>
          <h2>A request <em>restores</em>.</h2>
          <p>
            The bytes map from disk straight back into memory and the guest
            resumes mid-thought. The readiness wait that cost
            <span class="num big">{coldWait.ms.toLocaleString()}&nbsp;ms</span>
            cold takes
            <span class="num big"
              >{firstRestore.phases
                .find((p) => p.name === "guest_wait_ready")
                .ms.toFixed(1)}&nbsp;ms</span
            >
            warm. The whole wake-up:
            <span class="num ember-text"
              >~{Math.round(meanSnapshotRestoreMs)}&nbsp;ms</span
            >.
          </p>
        </div>
        <div class="caption" bind:this={capRepeatEl}>
          <h2>And again. And again.</h2>
          <p>
            Each restore is a fresh, isolated VM from the same frozen image,
            then discarded. {restores.length} real recorded runs, end to end, including
            auth and code execution.
          </p>
        </div>

        <!-- machinery -->
        <div class="machine" bind:this={machineEl}>
          <div class="vm" bind:this={vmEl}>
            <div class="vm-head">
              <span class="lbl">microVM &middot; guest RAM</span>
              <span class="state run" bind:this={vmstateEl}>booting</span>
            </div>
            <div class="ram">
              <div class="cells" bind:this={cellsEl}></div>
              <div class="glow" bind:this={ramGlowEl}></div>
            </div>
            <div class="vm-foot">
              <span>kata-fc &middot; node-local</span>
              <span class="num">vcpu 1 &middot; jailed</span>
            </div>
          </div>
          <div class="disk" bind:this={diskEl}>
            <div class="disk-rail"></div>
            <div class="files">
              <div class="file mem" bind:this={fileMemEl}>
                <div class="fname">base.mem</div>
                <div class="fdesc">every page of guest RAM</div>
                <div class="fill" bind:this={fillMemEl}></div>
              </div>
              <div class="file" bind:this={fileVmsEl}>
                <div class="fname">base.vmstate</div>
                <div class="fdesc">device + vcpu state</div>
                <div class="fill" bind:this={fillVmsEl}></div>
              </div>
            </div>
          </div>
          {#each Array(10) as _, i (i)}
            <div class="page-dot" bind:this={dotEls[i]}></div>
          {/each}
        </div>

        <!-- real recorded runs -->
        <div class="chips" bind:this={chipsEl} style="opacity: 0">
          {#each restores as r, i (i)}
            <div class="chip" bind:this={chipEls[i]}>
              <small>run {String(i + 1).padStart(2, "0")}</small>
              <span class="ms num">{r.total.toFixed(1)} ms</span>
            </div>
          {/each}
        </div>

        <!-- timing track -->
        <div class="track" bind:this={trackEl}>
          <div class="row">
            <div class="row-head">
              <span class="lbl"
                >cold: build the base snapshot (once, at daemon startup)</span
              >
              <span class="total num" bind:this={coldTotalEl}>0 ms</span>
            </div>
            <div class="barwrap">
              <div
                class="bar"
                bind:this={coldBarEl}
                style="clip-path: inset(0 100% 0 0)"
              >
                <div
                  class="seg boot"
                  style="width: {segByName.firecracker_boot.fraction *
                    100}%; min-width: 2px"
                ></div>
                <div
                  class="seg wait"
                  style="width: {segByName.guest_wait_ready.fraction * 100}%"
                ></div>
                <div
                  class="seg save"
                  style="width: {segByName.snapshot_save.fraction * 100}%"
                ></div>
              </div>
              <span class="bar-note" style="left: 34%"
                >waiting for a useful guest · {coldWait.ms.toLocaleString()} ms</span
              >
            </div>
          </div>
          <div class="row" bind:this={restoreRowEl} style="opacity: 0">
            <div class="row-head">
              <span class="lbl"
                >warm: restore from the snapshot (every request)</span
              >
              <span class="total num ember-text"
                >{Math.round(firstRestore.total)} ms</span
              >
            </div>
            <div class="barwrap">
              <div
                class="bar"
                bind:this={restoreBarEl}
                style="clip-path: inset(0 100% 0 0)"
              >
                <div
                  class="seg restore"
                  style="width: {restoreBarFraction(firstRestore, cold) *
                    100}%; min-width: 3px"
                ></div>
              </div>
              <span class="bar-note outside" style="left: 1.2%"
                >same scale. really.</span
              >
            </div>
          </div>
          <div class="zoom" bind:this={zoomEl}>
            <span class="lbl"
              >that sliver, magnified &middot; run #1 of {restores.length}</span
            >
            <div class="zbar">
              {#each firstRestore.phases.filter((p) => p.ms > 0) as p (p.name)}
                <div
                  class="zseg"
                  style="width: {(p.ms / firstRestore.total) *
                    100}%; background: var(--fc-phase-{p.name.replace(
                    /_/g,
                    '-',
                  )})"
                ></div>
              {/each}
            </div>
            <div class="legend">
              {#each firstRestore.phases.filter((p) => p.ms > 0) as p (p.name)}
                <span
                  class="sw"
                  style="background: var(--fc-phase-{p.name.replace(
                    /_/g,
                    '-',
                  )})"
                ></span>
                <span>{PHASE_HUMAN[p.name]}</span>
                <span class="ms num">{p.ms.toFixed(1)} ms</span>
              {/each}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ==================== REPLAY ==================== -->
  <section class="replay">
    <h2>Feel it. Restore one now.</h2>
    <p>
      This button replays one of the {restores.length} recorded runs at its true speed.
      No cluster is touched: the timings below were captured from the live daemon
      and baked into this page.
    </p>
    <ReplayWidget {restores} {PHASE_HUMAN} />
  </section>

  <!-- ==================== REDUCED-MOTION FALLBACK ==================== -->
  <div class="static-story">
    <section>
      <h1 class="static-hero-title">Boot once. Restore forever.</h1>
      <p class="static-muted">
        This site runs untrusted code in Firecracker microVMs. Building the
        first one took {(cold.total / 1000).toFixed(1)} seconds. Every sandbox since
        has woken from disk in about
        {Math.round(meanSnapshotRestoreMs)} milliseconds.
      </p>
    </section>
    <section>
      <h2>
        Cold, once at startup: <span class="num"
          >{cold.total.toLocaleString()} ms</span
        >
      </h2>
      <p class="static-muted num">
        {#each cold.phases as p, i (p.name)}{i > 0 ? " · " : ""}{p.name}
          {p.ms.toFixed(1)} ms{/each}
      </p>
    </section>
    <section>
      <h2>
        Warm, every request:
        <span class="num ember-text"
          >{restoreTotalMin.toFixed(0)}&ndash;{restoreTotalMax.toFixed(0)} ms</span
        >
      </h2>
      <p class="static-muted">
        The snapshot restore itself is a steady ~{Math.round(
          meanSnapshotRestoreMs,
        )} ms across all {restores.length} recorded runs.
      </p>
    </section>
  </div>
</div>

<style>
  .fcstory {
    background: var(--fc-ground);
    color: var(--fc-ink);
    font-family: var(--fc-sans);
    -webkit-font-smoothing: antialiased;
    overflow-x: clip;
  }

  .fcstory :global(.num) {
    font-family: var(--fc-mono);
    font-variant-numeric: tabular-nums;
  }
  .fcstory :global(.lbl) {
    font-family: var(--fc-mono);
    font-size: 13px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--fc-muted);
  }
  .frost-text {
    color: var(--fc-frost);
  }
  /* Inline ember text sits on the cream --fc-ground; the brand --fc-ember
     (#e05c26) is only ~3.3:1 there and fails WCAG AA. --fc-ember-deep
     (#b7461a) is ~4.8:1, still unmistakably the warm accent. Ember stays the
     brand color for fills (segments, chips) where it is not body text. */
  .ember-text {
    color: var(--fc-ember-deep);
  }

  /* The scrubbed stage is the default, so the server render and the
     JS-enabled first paint are identical: no post-hydration display swap
     means no layout shift and no "static story flashes first" flicker. The
     static stacked fallback shows only for reduced-motion (the browser
     resolves the media query at first paint, so still no shift) or for no-JS
     (the <noscript> reveal in +page.svelte). */
  .static-story {
    display: none;
  }
  @media (prefers-reduced-motion: reduce) {
    .scroller {
      display: none;
    }
    .static-story {
      display: block;
    }
  }

  /* ---------- topbar ---------- */
  .topbar {
    position: fixed;
    inset: 0 0 auto 0;
    z-index: 40;
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px 28px;
    font-family: var(--fc-mono);
    font-size: 12.5px;
    color: var(--fc-muted);
    pointer-events: none;
    transition: opacity 0.4s;
  }
  .topbar strong {
    color: var(--fc-ink);
    font-weight: 600;
  }
  /* The wordmark links home. The topbar itself stays pointer-events:none so it
     never intercepts the scroll story; only this link opts back in. */
  .topbar .brand {
    pointer-events: auto;
    color: inherit;
    text-decoration: none;
    border-radius: 4px;
  }
  .topbar .brand:hover strong,
  .topbar .brand:not(:has(strong)):hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .topbar .brand:focus-visible {
    outline: 2px solid var(--fc-ember-deep);
    outline-offset: 3px;
  }

  /* ---------- scroll story ---------- */
  .scroller {
    height: 860vh;
    position: relative;
  }
  .stage {
    position: sticky;
    top: 0;
    height: 100dvh;
    overflow: hidden;
  }
  .frame {
    position: relative;
    height: 100%;
    max-width: 1560px;
    margin: 0 auto;
  }

  .caption {
    position: absolute;
    left: 4vw;
    top: 50%;
    /* base = vertical centering; --cap-enter is the per-frame entrance offset
       written by cap() (defaults to 0 before JS runs / for SSR). */
    transform: translateY(calc(-50% + var(--cap-enter, 0px)));
    width: min(36vw, 560px);
    opacity: 0;
    z-index: 10;
    will-change: opacity, transform;
    pointer-events: none;
  }
  .caption h2 {
    font-size: clamp(30px, 3.6vw, 54px);
    line-height: 1.1;
    font-weight: 750;
    letter-spacing: -0.02em;
    margin: 0 0 18px;
    text-wrap: balance;
  }
  .caption p {
    font-size: clamp(17px, 1.35vw, 20px);
    line-height: 1.55;
    color: var(--fc-muted);
    margin: 0;
    max-width: 40ch;
  }
  .caption :global(.big) {
    color: var(--fc-ink);
    font-weight: 600;
  }
  .caption em {
    font-style: normal;
    color: var(--fc-ember);
  }
  .caption .cool {
    color: var(--fc-frost);
  }

  /* hero */
  .hero {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    z-index: 12;
    will-change: opacity, transform;
  }
  .hero h1 {
    font-size: clamp(44px, 7vw, 104px);
    font-weight: 800;
    letter-spacing: -0.035em;
    line-height: 1;
    margin: 0 0 26px;
    text-wrap: balance;
  }
  .hero h1 .cold-word {
    color: var(--fc-frost);
  }
  .hero h1 .warm-word {
    color: var(--fc-ember);
  }
  .hero p {
    font-size: clamp(16px, 1.5vw, 21px);
    color: var(--fc-muted);
    margin: 0;
    max-width: 54ch;
    line-height: 1.6;
  }
  .hero :global(.big) {
    color: var(--fc-ink);
    font-weight: 600;
  }
  .hero .cue {
    margin-top: 60px;
    font-family: var(--fc-mono);
    font-size: 11.5px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    /* --fc-faint (#7d838e) is only ~3.4:1 on the cream ground and fails AA;
       --fc-muted (#525965) is ~6.3:1. The cue also pulses to full opacity. */
    color: var(--fc-muted);
    animation: fc-cue 2.2s ease-in-out infinite;
  }
  @keyframes fc-cue {
    0%,
    100% {
      transform: translateY(0);
      opacity: 0.55;
    }
    50% {
      transform: translateY(7px);
      opacity: 1;
    }
  }

  /* ---------- machinery ---------- */
  .machine {
    position: absolute;
    right: 4vw;
    top: 43%;
    transform: translateY(-50%);
    width: min(50vw, 780px);
    z-index: 5;
  }

  .vm {
    position: relative;
    border: 1px solid var(--fc-line);
    border-radius: 16px;
    background: var(--fc-panel);
    padding: 24px 24px 20px;
    opacity: 0;
    will-change: opacity, transform;
    box-shadow: var(--fc-shadow);
  }
  .vm-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }
  .vm-head .state {
    font-family: var(--fc-mono);
    font-size: 13px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    font-weight: 700;
    padding: 5px 13px;
    border-radius: 999px;
  }
  :global(.state.run) {
    color: var(--fc-ember-deep);
    background: color-mix(in srgb, var(--fc-ember) 10%, transparent);
  }
  :global(.state.frozen) {
    color: var(--fc-frozen-text);
    background: color-mix(in srgb, var(--fc-frost) 12%, transparent);
  }

  .ram {
    position: relative;
    /* Height scales with the viewport so the whole machine (panel + disk
       shelf) gets shorter on a laptop window and the disk cards do not push
       down into the timing track / zoom inset. */
    height: clamp(150px, 30vh, 400px);
    border-radius: 8px;
    overflow: hidden;
    background: var(--fc-ram-bg);
  }
  /* one div per memory page; state flips a class, CSS transitions do the
     fade */
  .cells {
    position: absolute;
    inset: 10px;
    display: grid;
    gap: 3px;
  }
  .cells :global(.cell) {
    border-radius: 2.5px;
    background: var(--fc-cell-idle-bg);
    opacity: 0.45;
    transform: scale(0.8);
    transition:
      background-color 0.32s ease,
      opacity 0.3s ease,
      transform 0.3s ease;
  }
  .cells :global(.cell.hot) {
    background: var(--hot);
    opacity: 1;
    transform: scale(1);
  }
  .cells :global(.cell.cold) {
    background: var(--cold);
    opacity: 1;
    transform: scale(1);
  }
  .ram .glow {
    position: absolute;
    inset: -30%;
    background: radial-gradient(
      50% 50% at 50% 50%,
      color-mix(in srgb, var(--fc-ember) 18%, transparent),
      transparent 70%
    );
    opacity: 0;
    will-change: opacity;
    pointer-events: none;
  }

  .vm-foot {
    display: flex;
    justify-content: space-between;
    margin-top: 12px;
    font-family: var(--fc-mono);
    font-size: 13px;
    color: var(--fc-muted);
  }

  /* disk shelf */
  .disk {
    margin-top: 40px;
    opacity: 0;
    will-change: opacity, transform;
    position: relative;
  }
  .disk-rail {
    height: 1px;
    background: var(--fc-line);
    margin-bottom: 16px;
    position: relative;
  }
  .disk-rail::after {
    content: "DISK";
    position: absolute;
    right: 0;
    top: -20px;
    font-family: var(--fc-mono);
    font-size: 10.5px;
    letter-spacing: 0.15em;
    color: var(--fc-faint);
  }
  .files {
    display: flex;
    gap: 16px;
  }
  .file {
    border: 1px solid var(--fc-frost-dim);
    border-radius: 8px;
    padding: 14px 18px;
    background: var(--fc-panel);
    opacity: 0;
    transform: translateY(-14px);
    will-change: opacity, transform;
    flex: 0 0 auto;
    box-shadow: var(--fc-shadow);
  }
  .file .fname {
    font-family: var(--fc-mono);
    font-size: 13.5px;
    color: var(--fc-frost);
    font-weight: 600;
  }
  .file .fdesc {
    font-size: 13px;
    color: var(--fc-muted);
    margin-top: 4px;
  }
  .file.mem {
    flex: 1 1 auto;
  }
  .file .fill {
    height: 5px;
    border-radius: 3px;
    background: linear-gradient(90deg, var(--fc-frost-dim), var(--fc-frost));
    margin-top: 10px;
    transform: scaleX(0);
    transform-origin: left;
    will-change: transform;
  }

  /* particles (memory pages in flight) */
  .machine :global(.page-dot) {
    position: absolute;
    width: 16px;
    height: 11px;
    border-radius: 2.5px;
    background: var(--fc-frost);
    opacity: 0;
    z-index: 8;
    will-change: transform, opacity;
    box-shadow: 0 1px 6px color-mix(in srgb, var(--fc-frost) 35%, transparent);
  }
  .machine :global(.page-dot.hot) {
    background: var(--fc-ember);
    box-shadow: 0 1px 6px color-mix(in srgb, var(--fc-ember) 40%, transparent);
  }

  /* ---------- timing track ---------- */
  .track {
    position: absolute;
    left: 4vw;
    right: 4vw;
    bottom: 6vh;
    z-index: 9;
    opacity: 0;
    will-change: opacity;
  }
  .track .row {
    margin-bottom: 22px;
  }
  .track .row-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 10px;
  }
  .track .row-head .total {
    font-family: var(--fc-mono);
    font-size: 20px;
    color: var(--fc-ink);
    font-weight: 700;
  }
  .barwrap {
    position: relative;
    height: 44px;
    border-radius: 8px;
    background: var(--fc-bar-track-bg);
    overflow: hidden;
    border: 1px solid var(--fc-line-soft);
  }
  .bar {
    position: absolute;
    inset: 0;
    display: flex;
    will-change: clip-path;
  }
  .seg {
    height: 100%;
  }
  .seg.boot {
    background: var(--fc-seg-boot);
  }
  .seg.wait {
    background: linear-gradient(
      90deg,
      var(--fc-seg-wait-a),
      var(--fc-seg-wait-b)
    );
  }
  .seg.save {
    background: var(--fc-seg-save);
  }
  .seg.restore {
    background: var(--fc-seg-restore);
  }
  .bar-note {
    position: absolute;
    font-family: var(--fc-mono);
    font-size: 13px;
    color: var(--fc-on-color);
    top: 50%;
    transform: translateY(-50%);
    white-space: nowrap;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
  }
  .bar-note.outside {
    color: var(--fc-faint);
    text-shadow: none;
  }

  /* magnifier inset for the restore sliver */
  .zoom {
    position: absolute;
    right: 0;
    bottom: 200px;
    width: min(400px, 42vw);
    background: var(--fc-panel);
    border: 1px solid var(--fc-line);
    border-radius: 10px;
    padding: 16px 18px;
    opacity: 0;
    transform: translateY(10px);
    will-change: opacity, transform;
    z-index: 11;
    box-shadow: var(--fc-shadow);
  }
  .zoom .lbl {
    margin-bottom: 10px;
    display: block;
  }
  .zoom .zbar {
    display: flex;
    height: 18px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--fc-bar-track-bg);
  }
  .zoom .zseg {
    height: 100%;
  }
  .zoom .legend {
    display: grid;
    grid-template-columns: 12px 1fr auto;
    gap: 6px 10px;
    margin-top: 12px;
    font-family: var(--fc-mono);
    font-size: 13px;
    color: var(--fc-muted);
    align-items: center;
  }
  .zoom .sw {
    width: 10px;
    height: 10px;
    border-radius: 2px;
  }
  .zoom .ms {
    color: var(--fc-ink);
    font-weight: 600;
  }

  /* chips: recorded runs */
  .chips {
    position: absolute;
    right: 4vw;
    top: 50%;
    transform: translateY(-50%);
    width: min(50vw, 780px);
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    z-index: 6;
  }
  .chip {
    border: 1px solid var(--fc-ember-dim);
    border-radius: 8px;
    background: var(--fc-panel);
    padding: 14px 16px;
    font-family: var(--fc-mono);
    font-size: 15px;
    color: var(--fc-ink);
    opacity: 0;
    transform: scale(0.86);
    will-change: opacity, transform;
    box-shadow: var(--fc-shadow);
  }
  .chip small {
    display: block;
    font-size: 11px;
    letter-spacing: 0.1em;
    color: var(--fc-muted);
    margin-bottom: 4px;
  }
  .chip .ms {
    color: var(--fc-ember);
    font-weight: 600;
  }

  /* ---------- replay section ---------- */
  .replay {
    max-width: 920px;
    margin: 0 auto;
    padding: 8vh 24px;
    min-height: 100dvh;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .replay h2 {
    font-size: clamp(30px, 3.4vw, 46px);
    font-weight: 750;
    letter-spacing: -0.02em;
    margin: 0 0 12px;
    text-wrap: balance;
  }
  .replay > p {
    color: var(--fc-muted);
    line-height: 1.6;
    max-width: 60ch;
    margin: 0 0 36px;
    font-size: 17px;
  }

  /* ---------- fallback ---------- */
  .static-story {
    max-width: 760px;
    margin: 0 auto;
    padding: 12vh 24px;
  }
  .static-story section {
    margin-bottom: 9vh;
  }
  .static-hero-title {
    font-size: 40px;
    letter-spacing: -0.02em;
  }
  .static-muted {
    color: var(--fc-muted);
    line-height: 1.6;
  }
  @media (prefers-reduced-motion: reduce) {
    .hero .cue {
      animation: none;
    }
  }

  /* ---------- short-viewport (laptop / low-res) compaction ---------- */
  /* Separate concern from the mobile bug below: the desktop three-region
     spread (caption left, machine right, track bottom, zoom inset lower-right)
     was laid out for a TALL viewport. On any shorter window three things on the
     right column collide vertically: the centered machine's disk shelf reaches
     the bottom timing track (base.vmstate lands on the cold total), and the
     .zoom inset overlaps the disk shelf. This is a HEIGHT breakpoint (not
     width) because the failure is purely vertical, so it fires the same on a 4K
     monitor zoomed in as on a small laptop. One threshold covers the whole
     band: piecemeal cutoffs left a gap just above them (e.g. a 14" MacBook
     window at ~880px cleared an 840px cutoff yet still collided).

     Below the threshold we compact the entire right column: a smaller, higher
     machine so its disk shelf clears the track; a track pulled tight to the
     bottom with shorter rows; and the zoom dropped (its per-phase breakdown is
     reproduced verbatim by the replay console below the fold, exactly as mobile
     already does, so hiding it loses no information). */

  /* The zoom inset needs MORE headroom than the rest: it sits below the disk
     shelf and only clears it on a genuinely tall display (~1400px+, i.e. a
     1440p/4K monitor). Its own, higher threshold; between here and 1100px the
     full machine already sits high enough that the disk shelf clears the track
     on its own, so only the zoom needs dropping in that band. */
  @media (max-height: 1400px) {
    .zoom {
      display: none;
    }
  }

  @media (max-height: 1100px) {
    /* smaller + higher machine so the disk shelf sits well above the track */
    .machine {
      top: 40%;
    }
    .ram {
      height: clamp(140px, 26vh, 300px);
    }
    /* track pulled tight to the bottom with shorter rows */
    .track {
      bottom: 3vh;
    }
    .track .row {
      margin-bottom: 12px;
    }
    .track .row-head {
      margin-bottom: 6px;
    }
    .barwrap {
      height: 36px;
    }
    .caption h2 {
      font-size: clamp(26px, 3.2vw, 40px);
      margin-bottom: 12px;
    }
  }

  /* ---------- mobile ---------- */
  /* A phone has one column, not three, so the desktop caption-left /
     machine-right / track-bottom spread collapses into overlap. Stack the
     story into three horizontal bands with VIEWPORT-RELATIVE regions that
     cannot overlap whatever the content does:
       - machine / chips: top 2svh, its band ending above the track
       - timing track:    top 42svh (always below the machine band)
       - narration:       bottom sheet, at most 32svh tall, pinned to bottom
     The old layout anchored the machine from the top (height driven by the
     disk shelf that fades in mid-story) and the track from the bottom, so when
     the disk cards appeared the two bands met in the middle. Anchoring every
     band to the viewport removes that content-dependent collision.

     The bands MUST use the same viewport unit as the .stage they live inside.
     The stage is sized in dvh (it grows/shrinks with the url bar); anchoring
     the bands in plain `vh` (which many mobile browsers resolve to the LARGE,
     url-bar-hidden viewport) positioned them as if the screen were taller than
     the visible stage, so the track and the bottom caption sheet collided
     whenever the url bar was showing. `svh` is the SMALL (url-bar-visible)
     viewport, so the bands fit the worst case; when the bar hides there is just
     extra slack at the bottom instead of an overlap. The machine is also
     compacted (smaller RAM, description-free disk cards) so it fits its band
     even on short phones. */
  @media (max-width: 820px) {
    .hero h1 {
      font-size: 44px;
    }

    /* visual (machine / chips) band: top 2svh, compacted to fit above the
       timing track's 42svh top edge */
    .machine {
      right: 5vw;
      left: 5vw;
      top: 2svh;
      transform: none;
      width: auto;
    }
    .vm {
      padding: 16px 16px 14px;
    }
    .vm-head {
      margin-bottom: 10px;
    }
    .ram {
      height: clamp(80px, 10svh, 120px);
    }
    .vm-foot {
      margin-top: 10px;
    }
    .disk {
      margin-top: 12px;
    }
    .files {
      gap: 12px;
    }
    .file {
      padding: 9px 12px;
    }
    /* the description line is desktop polish; drop it on mobile so the disk
       shelf stays inside the machine band */
    .file .fdesc {
      display: none;
    }
    .chips {
      right: 5vw;
      left: 5vw;
      top: 2svh;
      transform: none;
      width: auto;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
    }
    .chip {
      padding: 11px 12px;
      font-size: 13px;
    }

    /* timing track: fixed band below the machine, clear of the caption sheet */
    .track {
      top: 42svh;
      bottom: auto;
      left: 5vw;
      right: 5vw;
    }
    .track .row {
      margin-bottom: 10px;
    }
    /* the warm (second) row's trailing margin is dead space that only eats into
       the caption sheet's clearance on the shortest phones (:last-of-type would
       match the hidden .zoom div, not this row) */
    .track .row + .row {
      margin-bottom: 0;
    }
    .track .row-head .total {
      font-size: 17px;
    }
    .barwrap {
      height: 30px;
    }

    /* the magnifier inset has no room on a phone; the replay console below
       shows the same per-phase breakdown */
    .zoom {
      display: none;
    }

    /* caption becomes a bottom sheet with a scrim, so the narration never
       collides with the machine or the bars behind it */
    .caption {
      left: 0;
      right: 0;
      bottom: 0;
      top: auto;
      /* bottom sheet: no centering, only the per-frame entrance offset. This
         overrides the desktop translateY(-50%) so the sheet stays pinned to the
         bottom instead of being lifted up over the timing track. */
      transform: translateY(var(--cap-enter, 0px));
      width: auto;
      max-height: 32svh;
      overflow: hidden;
      padding: 40px 5vw calc(3svh + 6px);
      background: linear-gradient(
        180deg,
        transparent 0%,
        var(--fc-ground) 22%,
        var(--fc-ground) 100%
      );
    }
    .caption h2 {
      font-size: 22px;
      margin-bottom: 8px;
    }
    .caption p {
      font-size: 14.5px;
      line-height: 1.45;
      max-width: none;
    }
  }
</style>
