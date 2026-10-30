<script>
  import { onDestroy } from "svelte";
  import Button from "@homelab/design-system/components/Button.svelte";

  // Baked measurements only. Svelte updates once per run; the animation
  // changes just the fill transform and is cancelled when navigation unmounts.
  let { restores, PHASE_HUMAN } = $props();
  let runIdx = $state(0);
  let count = $state(0);
  let playing = $state(false);
  let slowmo = $state(false);
  let finalMs = $state("");
  let showTable = $state(false);
  let currentRun = $derived(restores[runIdx % restores.length]);
  let displayedLabel = $state("");
  let rFillEl;
  let animation;

  const phaseColorVar = (name) => `var(--fc-phase-${name.replace(/_/g, "-")})`;

  function replay() {
    if (playing) return;
    // Advance only when starting the next replay so the result and phase
    // table continue to describe the completed run, including run 12.
    runIdx = count % restores.length;
    const run = restores[runIdx];
    playing = true;
    const mult = slowmo ? 40 : 1;
    displayedLabel = `run ${runIdx + 1} of ${restores.length}${slowmo ? " (40× slower)" : ""}`;
    finalMs = "";
    showTable = false;
    rFillEl.style.transform = "scaleX(0)";

    function finish() {
      count++;
      finalMs = run.total.toFixed(1) + " ms";
      showTable = true;
      playing = false;
      animation = undefined;
    }

    // Read on each invocation: a preference change never requires remounting.
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      rFillEl.style.transform = "scaleX(1)";
      finish();
      return;
    }
    const t0 = performance.now();
    function tick(now) {
      const p = Math.min(Math.max((now - t0) / (run.total * mult), 0), 1);
      rFillEl.style.transform = `scaleX(${p})`;
      if (p < 1) animation = requestAnimationFrame(tick);
      else finish();
    }
    animation = requestAnimationFrame(tick);
  }

  onDestroy(() => {
    if (animation !== undefined) cancelAnimationFrame(animation);
  });
</script>

<div class="console">
  <div class="console-top replay-controls">
    <Button disabled={playing} onclick={replay} aria-busy={playing}>
      &#9654;&nbsp; Restore again
    </Button>
    <label class="slowmo">
      <input type="checkbox" bind:checked={slowmo} disabled={playing} /> slow motion
      (40&times;)
    </label>
    <span class="counter">restores: <b class="num">{count}</b></span>
  </div>
  <div class="replay-bar" aria-hidden="true">
    <div class="replay-fill" bind:this={rFillEl}>
      {#each currentRun.phases.filter((p) => p.ms > 0) as phase (phase.name)}
        <span
          style={`width: ${(phase.ms / currentRun.total) * 100}%; background: ${phaseColorVar(phase.name)}`}
        ></span>
      {/each}
    </div>
  </div>
  <div class="replay-readout" role="status" aria-atomic="true">
    <span class="num">{displayedLabel || `run – of ${restores.length}`}</span>
    <span class="final num">{finalMs}</span>
  </div>
  {#if showTable}
    <table class="phase-table">
      <caption class="sr-only">{displayedLabel} phases</caption>
      <tbody>
        {#each currentRun.phases as phase (phase.name)}
          <tr>
            <th scope="row">
              <span
                class="sw"
                aria-hidden="true"
                style={`background: ${phaseColorVar(phase.name)}`}
              ></span>
              {phase.name}
              <span class="phase-meaning">{PHASE_HUMAN[phase.name]}</span>
            </th>
            <td class="num">{phase.ms.toFixed(1)} ms</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
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
  .console-top {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem 1rem;
    padding: 1rem;
    border-top: 1px solid var(--stroke);
    font: 0.75rem/1.5 var(--font-code);
  }
  .slowmo {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 44px;
    cursor: pointer;
  }
  input {
    accent-color: var(--accent-ink);
    width: 1.1rem;
    height: 1.1rem;
  }
  input:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .replay-bar {
    height: 1.25rem;
    background: var(--band);
    border-block: 1px solid var(--stroke);
  }
  .replay-fill {
    display: flex;
    height: 100%;
    transform: scaleX(0);
    transform-origin: left;
  }
  .replay-readout {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.5rem;
    min-height: 3rem;
    padding: 0.75rem 1rem;
    font-size: 0.8rem;
  }
  .phase-table {
    width: 100%;
    border-collapse: collapse;
    font: 0.75rem/1.5 var(--font-code);
  }
  th,
  td {
    border-top: 1px solid var(--line);
    padding: 0.6rem 1rem;
    text-align: left;
    vertical-align: top;
  }
  th {
    overflow-wrap: anywhere;
    font-weight: 400;
  }
  td {
    white-space: nowrap;
    border-left: 1px solid var(--stroke);
  }
  .phase-meaning {
    display: block;
    color: var(--ink-2);
  }
  .sw {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
  }
  .provenance {
    padding: 1rem;
    border-top: 1px solid var(--stroke);
    color: var(--ink-2);
    font: 0.75rem/1.6 var(--font-code);
    overflow-wrap: anywhere;
  }
  .provenance a {
    color: var(--accent-ink);
    text-underline-offset: 0.2em;
  }
</style>
