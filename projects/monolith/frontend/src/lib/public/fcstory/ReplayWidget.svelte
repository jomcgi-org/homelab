<script>
  // The replay console: "restore again" cycles through the real recorded
  // restore runs, animating a fill bar over exactly that run's real
  // millisecond duration (a 43ms run animates for 43ms). Client-side only,
  // driven entirely by the `restores` prop (baked static data, no fetch).
  // Ported from the reference mockup's replay console
  // (./reference-mockup.html, the <section class="replay"> markup + its
  // trailing <script> block).
  let { restores, PHASE_HUMAN } = $props();

  // `restores` is static baked data (props never change after mount), so
  // capturing its initial value here is safe; the function wrapper just
  // keeps the svelte compiler from flagging a direct prop read as the
  // initializer of a $state declaration.
  function initialRun() {
    return restores[0];
  }
  function initialLabel() {
    return `run – of ${restores.length}`;
  }

  let runIdx = $state(0);
  let count = $state(0);
  let playing = $state(false);
  let slowmo = $state(false);
  let finalMs = $state("");
  let showTable = $state(false);
  let currentRun = $state(initialRun());
  let displayedLabel = $state(initialLabel());

  let rFillEl;

  function phaseColorVar(name) {
    return `var(--fc-phase-${name.replace(/_/g, "-")})`;
  }

  function buildFill(run) {
    if (!rFillEl) return;
    rFillEl.innerHTML = "";
    run.phases.forEach((p) => {
      if (p.ms <= 0) return;
      const s = document.createElement("div");
      s.className = "rf";
      s.style.width = (p.ms / run.total) * 100 + "%";
      s.style.background = phaseColorVar(p.name);
      rFillEl.appendChild(s);
    });
  }

  const reduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function replay() {
    if (playing) return;
    playing = true;
    const run = restores[runIdx % restores.length];
    currentRun = run;
    const mult = slowmo ? 40 : 1;
    buildFill(run);
    displayedLabel = `run ${(runIdx % restores.length) + 1} of ${restores.length}${slowmo ? " (40× slower)" : ""}`;
    finalMs = "";
    showTable = false;
    if (rFillEl) rFillEl.style.transform = "scaleX(0)";

    function finish() {
      count++;
      finalMs = run.total.toFixed(1) + " ms";
      showTable = true;
      runIdx++;
      playing = false;
    }

    if (reduced) {
      if (rFillEl) rFillEl.style.transform = "scaleX(1)";
      finish();
      return;
    }
    const t0 = performance.now();
    (function tick() {
      const p = Math.min(
        Math.max((performance.now() - t0) / (run.total * mult), 0),
        1,
      );
      if (rFillEl) rFillEl.style.transform = `scaleX(${p})`;
      if (p < 1) requestAnimationFrame(tick);
      else finish();
    })();
  }

  $effect(() => {
    buildFill(currentRun);
  });
</script>

<div class="console">
  <div class="console-top">
    <button class="btn" disabled={playing} onclick={replay}>
      &#9654;&nbsp; Restore again
    </button>
    <label class="slowmo">
      <input type="checkbox" bind:checked={slowmo} /> slow motion (40&times;)
    </label>
    <span class="counter">restores: <b class="num">{count}</b></span>
  </div>
  <div class="replay-bar">
    <div class="replay-fill" bind:this={rFillEl}></div>
  </div>
  <div class="replay-readout">
    <span class="num">{displayedLabel}</span>
    <span class="final num">{finalMs}</span>
  </div>
  <div class="phase-table" class:show={showTable}>
    {#each currentRun.phases as p (p.name)}
      <span class="sw" style="background: {phaseColorVar(p.name)}"></span>
      <span
        >{p.name}
        <span class="faint">&middot; {PHASE_HUMAN[p.name]}</span></span
      >
      <span class="pbarw">
        <span
          class="pbar"
          style="width: {(p.ms /
            Math.max(...currentRun.phases.map((x) => x.ms))) *
            100}%; background: {phaseColorVar(p.name)}"
        ></span>
      </span>
      <span class="pms num">{p.ms.toFixed(1)} ms</span>
    {/each}
  </div>
  <div class="provenance">
    Every number on this page is a real measurement exported by the daemon's own
    tracing, recorded and baked in at build time. Warm restores: {restores.length}
    consecutive live runs.
    <br />
    source:
    <a href="https://github.com/jomcgi/homelab/tree/main/projects/embervm"
      >projects/embervm</a
    >
  </div>
</div>

<style>
  .console {
    border: 1px solid var(--fc-line);
    border-radius: 16px;
    background: var(--fc-panel);
    padding: 32px;
    box-shadow: var(--fc-shadow);
  }
  .console-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
  }
  .btn {
    font-family: var(--fc-mono);
    font-size: 16px;
    letter-spacing: 0.04em;
    padding: 15px 32px;
    border-radius: 9px;
    border: 1.5px solid var(--fc-ember-deep);
    background: color-mix(in srgb, var(--fc-ember) 14%, transparent);
    color: var(--fc-ember-deep);
    cursor: pointer;
    font-weight: 700;
    transition: background 0.15s;
  }
  .btn:hover {
    background: color-mix(in srgb, var(--fc-ember) 26%, transparent);
  }
  .btn:focus-visible {
    outline: 2px solid var(--fc-ember-deep);
    outline-offset: 2px;
  }
  .btn[disabled] {
    opacity: 0.5;
    cursor: default;
  }
  .slowmo {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--fc-mono);
    font-size: 14px;
    color: var(--fc-muted);
  }
  .slowmo input {
    accent-color: var(--fc-ember);
  }
  .counter {
    font-family: var(--fc-mono);
    font-size: 14px;
    color: var(--fc-muted);
  }
  .counter b {
    color: var(--fc-ink);
    font-size: 20px;
  }
  .replay-bar {
    position: relative;
    height: 34px;
    border-radius: 7px;
    background: var(--fc-bar-track-bg);
    overflow: hidden;
    margin: 28px 0 10px;
    border: 1px solid var(--fc-line-soft);
  }
  .replay-fill {
    position: absolute;
    inset: 0;
    display: flex;
    transform: scaleX(0);
    transform-origin: left;
    will-change: transform;
  }
  .replay-fill :global(.rf) {
    height: 100%;
  }
  .replay-readout {
    display: flex;
    justify-content: space-between;
    font-family: var(--fc-mono);
    font-size: 13.5px;
    color: var(--fc-muted);
  }
  .replay-readout .final {
    color: var(--fc-ink);
    font-size: 19px;
    font-weight: 700;
  }
  .phase-table {
    margin-top: 24px;
    border-top: 1px solid var(--fc-line);
    padding-top: 16px;
    display: grid;
    grid-template-columns: 14px minmax(150px, max-content) 1fr auto;
    gap: 8px 14px;
    align-items: center;
    font-family: var(--fc-mono);
    font-size: 13.5px;
    color: var(--fc-muted);
    opacity: 0;
    transition: opacity 0.35s;
  }
  .phase-table.show {
    opacity: 1;
  }
  .phase-table .sw {
    width: 10px;
    height: 10px;
    border-radius: 2px;
  }
  .phase-table .faint {
    color: var(--fc-faint);
  }
  .phase-table .pbarw {
    height: 7px;
    border-radius: 3.5px;
    background: var(--fc-bar-track-bg);
    overflow: hidden;
  }
  .phase-table .pbar {
    display: block;
    height: 100%;
    border-radius: 3.5px;
  }
  .phase-table .pms {
    color: var(--fc-ink);
    font-weight: 600;
  }
  .provenance {
    margin-top: 36px;
    font-family: var(--fc-mono);
    font-size: 13px;
    color: var(--fc-muted);
    line-height: 1.7;
  }
  .provenance a {
    color: var(--fc-ember-deep);
  }
</style>
