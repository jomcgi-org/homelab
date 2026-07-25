<script>
  // Live hot/cold RAM cell grid for /ember/postgres, driven by the console's
  // real lifecycle state instead of scroll position. Same discipline as
  // fcstory/FcScrollStory.svelte: a grid of plain (non-reactive) cell divs,
  // per-cell jittered thresholds built once from Math.random() in onMount,
  // and a single rAF loop that writes className/style directly to element
  // refs so 60fps updates never touch Svelte's reactivity proxy. Palette hex
  // values live in ./ember-stage.css as CSS custom properties (bare hex in a
  // .svelte <style> block is semgrep-blocked).
  import { onMount } from "svelte";
  import { fade } from "svelte/transition";
  import "./ember-stage.css";

  /** @type {{ vmState?: string|null, totalSavedMibS?: number|null, stopwatchMs?: number, running?: boolean, wakePromise?: string, present?: number|null }} */
  let {
    vmState = null,
    totalSavedMibS = null,
    stopwatchMs = 0,
    running = false,
    wakePromise = "",
    present = null,
  } = $props();

  const WAKING = new Set(["relighting", "cold_booting", "starting"]);

  // Visual sweep durations, not measurements: the wake sweep aims at the
  // typical connect time so the ragged edge crosses the grid roughly as the
  // real wake happens; the bank sweep is a flat, deliberately quick cool-down.
  const WAKE_SWEEP_MS = 1400;
  const BANK_SWEEP_MS = 2200;
  // How fast the eased warmth `w` chases `target`. Small = tracks closely
  // (the sweeps above already own the pacing), just enough easing to avoid a
  // visible snap on state flips (banked -> serving with no run in between).
  const EASE_PER_MS = 0.012;

  const STATE_WORD = {
    banked: "Asleep",
    checkpointed: "Asleep",
    banking: "Falling asleep",
    relighting: "Waking",
    cold_booting: "Waking",
    starting: "Waking",
    serving: "Awake",
  };

  let reduced = $state(false);

  // ── Plain (non-reactive) per-frame refs ──
  let gridEl;
  let cells = [];
  let raf = null;
  let lastFrameAt = 0;
  let w = 0; // current eased warmth [0,1]

  // Sweep bookkeeping: reset once per state transition (tracked via
  // prevFrameState), read every frame.
  let prevFrameState = null;
  let sweepStartAt = 0;
  let sweepFromW = 0;

  // Directional damping: rises are accepted instantly (a click or a wake
  // should pivot the sweep immediately), but a transition toward cold only
  // takes effect after the cold-ish state has held for COLD_DEBOUNCE_MS.
  // During a wake-retry cycle the lifecycle can thrash relighting -> failed
  // -> relighting every second; without the debounce the grid flaps.
  const COLD_DEBOUNCE_MS = 1200;
  let coldSince = null;
  let acceptedState = null;

  // Reactive mirror of the damped state, written by the rAF loop only on
  // transitions, so the badge and hero flip in the SAME frame the color
  // scrub starts instead of announcing "Asleep" over a still-warm grid.
  let displayState = $state(null);

  function dampedState(now) {
    const raw = effectiveState();
    const warms = raw === "serving" || WAKING.has(raw ?? "");
    if (warms) {
      coldSince = null;
      acceptedState = raw;
      return raw;
    }
    if (coldSince == null) coldSince = now;
    if (now - coldSince >= COLD_DEBOUNCE_MS || acceptedState == null) {
      acceptedState = raw;
    }
    return acceptedState;
  }

  function buildCells() {
    // Grid sized from the container, same technique as FcScrollStory's
    // buildCells: ncols/nrows from clientWidth/clientHeight, each cell gets a
    // stable jittered threshold so the sweep tracks time but the edge is
    // ragged. Math.random() only ever runs here, from onMount/resize, never
    // at module/SSR eval time.
    if (!gridEl) return;
    const rw = gridEl.clientWidth;
    const rh = gridEl.clientHeight;
    const ncols = Math.max(16, Math.round(rw / 20));
    const nrows = Math.max(6, Math.round(rh / 20));
    gridEl.style.gridTemplateColumns = `repeat(${ncols},1fr)`;
    gridEl.style.gridTemplateRows = `repeat(${nrows},1fr)`;
    gridEl.innerHTML = "";
    cells = [];
    for (let i = 0; i < ncols * nrows; i++) {
      const col = i % ncols;
      const d = document.createElement("div");
      d.className = "es-cell";
      const hotHue = 5 + Math.random() * 10;
      const hotSat = 80 + Math.random() * 12;
      const hotLight = 44 + Math.random() * 12;
      const coldHue = 208 + Math.random() * 12;
      const coldSat = 52 + Math.random() * 14;
      const coldLight = 60 + Math.random() * 12;
      d.style.setProperty("--hot", `hsl(${hotHue} ${hotSat}% ${hotLight}%)`);
      d.style.setProperty(
        "--cold",
        `hsl(${coldHue} ${coldSat}% ${coldLight}%)`,
      );
      gridEl.appendChild(d);
      cells.push({
        el: d,
        th: (col + 0.2 + Math.random() * 1.3) / (ncols + 1.3),
        hot: false,
        twinkling: false,
      });
    }
  }

  let flickerTickAt = 0;

  // Optimistic wake: the moment a query is in flight against a cold VM, treat
  // the state as waking instead of waiting for the control plane -> status
  // cache -> poll chain to report it (~1-2s of dead air after the click). The
  // click IS the wake signal; if the run is refused (rate limiter, busy), the
  // console flips running off, the effective state falls back to banked, and
  // the eased warmth drifts back down without a snap.
  const COLD_STATES = new Set(["banked", "checkpointed"]);

  function effectiveState() {
    if (running && (vmState == null || COLD_STATES.has(vmState))) {
      return "relighting";
    }
    return vmState;
  }

  function targetFor(now, state) {
    if (state === "serving" || WAKING.has(state)) {
      // One rising sweep from wherever the transition caught the warmth to
      // full, always at sweep pace: a warm run answering in 80ms must not
      // snap the remaining fill, it keeps rolling to completion.
      const frac = Math.min(1, (now - sweepStartAt) / WAKE_SWEEP_MS);
      return sweepFromW + (1 - sweepFromW) * frac;
    }
    // Everything else (banking, banked, checkpointed, null, and unknown
    // lifecycle states like failed/evicted) cools down on the deliberate
    // sweep: sweepFromW was captured at the transition, so the grid visibly
    // banks warm -> cold instead of near-snapping, and a wedged state never
    // holds a hot grid under an ASLEEP badge.
    const frac = Math.min(1, (now - sweepStartAt) / BANK_SWEEP_MS);
    return sweepFromW * (1 - frac);
  }

  function frame(now) {
    const dt = lastFrameAt ? now - lastFrameAt : 16;
    lastFrameAt = now;

    const state = dampedState(now);
    if (state !== displayState) displayState = state;
    if (state !== prevFrameState) {
      // Entered a new (effective) state this frame: (re)start the sweep clock
      // from the current eased warmth, so a mid-sweep state flip (e.g. banking
      // interrupted by a new request, or an optimistic wake refused) never
      // snaps.
      prevFrameState = state;
      sweepStartAt = now;
      sweepFromW = w;
    }
    const target = targetFor(now, state);
    const k = 1 - Math.exp(-EASE_PER_MS * dt);
    w = w + (target - w) * k;

    for (const c of cells) {
      const hot = w > c.th;
      if (hot !== c.hot) {
        c.hot = hot;
        c.el.className = "es-cell" + (hot ? " es-hot" : "");
      }
    }

    if (vmState === "serving") {
      // Gentle per-cell twinkle: every tick a couple of hot cells start their
      // own one-shot shimmer with a randomized duration and clean up on
      // animationend. Never clears cells in batches: cancelling animations
      // mid-flight snapped them back in sync, which read as flashing.
      if (now - flickerTickAt > 900) {
        flickerTickAt = now;
        const hotCells = cells.filter((c) => c.hot && !c.twinkling);
        const n = Math.min(
          hotCells.length,
          Math.max(1, Math.round(cells.length * 0.008)),
        );
        for (let i = 0; i < n; i++) {
          const c = hotCells[Math.floor(Math.random() * hotCells.length)];
          if (!c || c.twinkling) continue;
          c.twinkling = true;
          c.el.style.setProperty(
            "--tw-dur",
            `${(1.4 + Math.random() * 1.2).toFixed(2)}s`,
          );
          c.el.style.setProperty("--flicker", "1");
          c.el.addEventListener(
            "animationend",
            () => {
              c.el.style.removeProperty("--flicker");
              c.twinkling = false;
            },
            { once: true },
          );
        }
      }
    }

    raf = requestAnimationFrame(frame);
  }

  let lastGridW = 0;
  function measure() {
    const gw = gridEl ? gridEl.clientWidth : 0;
    if (gw !== lastGridW) {
      lastGridW = gw;
      buildCells();
    }
  }

  let resizing = false;
  function onResize() {
    if (resizing) return;
    resizing = true;
    requestAnimationFrame(() => {
      measure();
      resizing = false;
    });
  }

  onMount(() => {
    reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return; // CSS + the static overlay below cover this case

    window.addEventListener("resize", onResize, { passive: true });
    requestAnimationFrame(() => {
      measure();
      lastFrameAt = 0;
      raf = requestAnimationFrame(frame);
    });

    return () => {
      window.removeEventListener("resize", onResize);
      if (raf != null) cancelAnimationFrame(raf);
    };
  });

  // Reactive mirror of effectiveState() for the derived display bits below
  // (the rAF loop reads effectiveState() directly each frame instead).
  let optimisticWaking = $derived(
    running && (vmState == null || COLD_STATES.has(vmState)),
  );

  // ── Reduced-motion static fallback: two-state cell fill, no rAF loop ──
  let staticHot = $derived(
    vmState === "serving" || WAKING.has(vmState ?? "") || optimisticWaking,
  );

  function ms(v) {
    if (v == null) return "–";
    return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
  }

  function humanize(n) {
    const abs = Math.abs(n);
    if (abs < 1000) return `${Math.round(n)}`;
    const units = [
      { value: 1e9, suffix: "B" },
      { value: 1e6, suffix: "M" },
      { value: 1e3, suffix: "K" },
    ];
    for (const { value, suffix } of units) {
      if (abs >= value) {
        const scaled = n / value;
        const decimals = Math.abs(scaled) < 100 ? 1 : 0;
        return `${scaled.toFixed(decimals)}${suffix}`;
      }
    }
    return `${Math.round(n)}`;
  }

  function gbHours(mibSeconds) {
    if (mibSeconds == null) return "–";
    const gbh = mibSeconds / 1024 / 3600;
    if (gbh < 10) return `${gbh.toFixed(1)} GB·h`;
    return `${humanize(gbh)} GB·h`;
  }

  // The badge/hero follow displayState (the damped, sweep-synchronized
  // state) when the rAF loop is running; reduced-motion has no loop, so it
  // falls back to the raw + optimistic derivation.
  let effDisplay = $derived(
    displayState ?? (optimisticWaking ? "relighting" : vmState),
  );

  let stateWord = $derived(STATE_WORD[effDisplay ?? ""] ?? "Asleep");

  let heroKind = $derived(
    effDisplay === "serving"
      ? "serving"
      : effDisplay != null && WAKING.has(effDisplay)
        ? "waking"
        : "cold",
  );

  // Live watchers pill: names the OTHER cause of warmth on this shared VM.
  // Without it a wake someone else triggered reads as a ghost ("why is it
  // glowing, I did nothing"); with it the crowd is the explanation. Solo is
  // its own message on purpose: "just you" is exactly when you WILL see the VM
  // sleep, so it reinforces the exhibit instead of looking lonely. Hidden
  // entirely until the first poll returns a count (present == null).
  let watchers = $derived(
    present == null
      ? null
      : present <= 1
        ? "just you here now"
        : `${present} here now`,
  );
</script>

<div class="ember-stage">
  <div class="es-grid" bind:this={gridEl} aria-hidden="true"></div>
  {#if reduced}
    <div
      class="es-grid es-static"
      class:es-static-hot={staticHot}
      aria-hidden="true"
    ></div>
  {/if}

  <div class="es-overlay">
    <div class="es-status-row">
      <span class="es-state-word">
        {#key stateWord}
          <span class="fade-swap" in:fade={{ duration: 260 }}>{stateWord}</span>
        {/key}
      </span>
      {#if watchers}
        <span class="es-watchers" in:fade={{ duration: 260 }}>
          <span class="es-watchers-dot" aria-hidden="true"></span>
          {#key watchers}
            <span class="fade-swap" in:fade={{ duration: 260 }}>{watchers}</span
            >
          {/key}
        </span>
      {/if}
    </div>
    <div class="es-hero">
      {#key heroKind}
        <div class="es-hero-inner" in:fade={{ duration: 260 }}>
          {#if heroKind === "cold"}
            <span class="es-hero-value">{gbHours(totalSavedMibS)}</span>
            <span class="es-hero-caption"
              >saved all-time by scaling to zero</span
            >
          {:else if heroKind === "waking"}
            <span class="es-hero-value">{ms(running ? stopwatchMs : null)}</span
            >
            <span class="es-hero-caption">waking up</span>
          {:else}
            <span class="es-hero-value">512 MiB</span>
            <span class="es-hero-caption">of Postgres live in memory</span>
          {/if}
        </div>
      {/key}
    </div>
  </div>
</div>

<style>
  .ember-stage {
    position: relative;
    width: 100%;
    height: clamp(150px, 24vh, 230px);
    border-radius: 14px;
    border: 1px solid var(--es-border);
    background: var(--es-panel);
    box-shadow: var(--em-shadow-soft);
    overflow: hidden;
  }

  .es-grid {
    position: absolute;
    inset: 8px;
    display: grid;
    gap: 2px;
    background: var(--es-grid-bg);
    border-radius: 4px;
  }

  .es-grid :global(.es-cell) {
    border-radius: 1.5px;
    background: var(--cold, var(--es-cell-idle-bg));
    opacity: 1;
  }

  .es-grid :global(.es-cell.es-hot) {
    background: var(--hot);
  }

  @media (prefers-reduced-motion: no-preference) {
    .es-grid :global(.es-cell) {
      transition: background-color 0.18s ease-out;
    }
    .es-grid :global(.es-cell[style*="--flicker"]) {
      animation: es-flicker var(--tw-dur, 1.8s) ease-in-out;
    }
    @keyframes es-flicker {
      0%,
      100% {
        opacity: 1;
        filter: brightness(1);
      }
      50% {
        opacity: 0.92;
        filter: brightness(1.08);
      }
    }
  }

  /* Reduced-motion fallback: a second, static grid painted with a plain
     two-vmState (cold/hot) fill using the same cell tokens, no per-cell
     randomness and no rAF loop. Only one of the two .es-grid elements is
     visible at a time (see the media query below). */
  .es-static :global(.es-cell) {
    background: var(--es-cell-idle-bg);
  }
  .es-static.es-static-hot :global(.es-cell) {
    background: var(--es-static-hot);
  }
  @media (prefers-reduced-motion: reduce) {
    .es-grid:not(.es-static) {
      display: none;
    }
  }
  @media (prefers-reduced-motion: no-preference) {
    .es-static {
      display: none;
    }
  }

  .es-overlay {
    position: relative;
    z-index: 1;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    padding: 20px;
    pointer-events: none;
    text-align: center;
  }

  .es-status-row {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
    gap: 8px;
  }

  .es-state-word {
    font-family: var(--em-mono, ui-monospace, monospace);
    font-size: 12.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--es-ink);
    background: var(--es-panel);
    border: 1px solid var(--es-border);
    box-shadow: var(--em-shadow-soft);
    padding: 4px 12px;
    border-radius: 999px;
  }

  /* Watchers pill: quieter than the state word (it is context, not the
     headline), same panel chip language. The dot is the only warm accent, a
     "live" tell borrowed from presence indicators everywhere. */
  .es-watchers {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--em-mono, ui-monospace, monospace);
    font-size: 11.5px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--es-ink);
    background: var(--es-panel);
    border: 1px solid var(--es-border);
    box-shadow: var(--em-shadow-soft);
    padding: 4px 11px;
    border-radius: 999px;
  }

  .es-watchers-dot {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--em-ember, currentColor);
    flex: none;
  }

  @media (prefers-reduced-motion: no-preference) {
    .es-watchers-dot {
      animation: es-watchers-pulse 2.2s ease-in-out infinite;
    }
    @keyframes es-watchers-pulse {
      0%,
      100% {
        opacity: 1;
      }
      50% {
        opacity: 0.35;
      }
    }
  }

  .es-hero {
    display: grid;
    place-items: center;
    min-height: 64px;
  }

  /* Outgoing and incoming hero states share the grid cell so the fade never
     stacks them vertically. */
  .es-hero-inner {
    grid-area: 1 / 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
  }

  .fade-swap {
    display: inline-block;
  }

  .es-hero-value {
    font-family: var(--em-mono, ui-monospace, monospace);
    font-size: clamp(24px, 3.4vw, 38px);
    letter-spacing: -0.02em;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--es-ink);
    background: var(--es-panel);
    box-shadow: var(--em-shadow-soft);
    padding: 2px 14px;
    border-radius: 8px;
  }

  .es-hero-caption {
    font-size: 13px;
    color: var(--es-ink);
    background: color-mix(in srgb, var(--es-panel) 94%, transparent);
    padding: 2px 10px;
    border-radius: 6px;
  }
</style>
