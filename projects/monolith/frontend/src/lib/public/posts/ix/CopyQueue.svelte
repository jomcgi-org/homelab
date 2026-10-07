<script>
  import { onMount } from "svelte";
  import Fig from "./Fig.svelte";
  import {
    ARRIVE_MS,
    PIECE_RECORDS,
    idleBefore,
    results,
    simulate,
  } from "./data-copy-queue.js";

  let mode = $state("all");
  const run = $derived(simulate(mode));
  const SPAN = 50; // ms shown
  const X0 = 74;
  // Drawn at 1:1 pixels: the viewBox follows the rendered width.
  let width = $state(360);
  const VBW = $derived(Math.max(300, Math.round(width)));
  const W = $derived(VBW - X0 - 8);
  const x = (ms) => X0 + (Math.min(ms, SPAN) / SPAN) * W;

  // Reveal the timeline left to right on each switch, unless motion is off.
  let shown = $state(SPAN);
  let reduced = true;
  let frame;
  onMount(() => {
    reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    return () => cancelAnimationFrame(frame);
  });
  function choose(next) {
    mode = next;
    cancelAnimationFrame(frame);
    if (reduced) return;
    const t0 = performance.now();
    shown = 0;
    const step = (now) => {
      shown = Math.min(SPAN, ((now - t0) / 1800) * SPAN);
      if (shown < SPAN) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
  }
  const fetch = $derived(run.blocks.find((b) => b.kind === "fetch"));
  const clipId = `cq-${Math.random().toString(36).slice(2)}`;
  const waitLabel = $derived(
    run.wait >= 1 ? `${Math.round(run.wait)} ms` : "under 1 ms",
  );
</script>

<Fig title="Prefetch in small batches so urgent copies don't wait">
  {#snippet controls()}
    <div class="seg" role="group" aria-label="How the stage-ahead is submitted">
      <button
        type="button"
        aria-pressed={mode === "all"}
        onclick={() => choose("all")}>All at once</button
      >
      <button
        type="button"
        aria-pressed={mode === "trickle"}
        onclick={() => choose("trickle")}>{PIECE_RECORDS} at a time</button
      >
    </div>
  {/snippet}

  <div class="plot" bind:clientWidth={width}>
    <svg
      viewBox={`0 0 ${VBW} 132`}
      role="img"
      aria-label={`Copy engine timeline: the current layer's copies wait ${waitLabel}.`}
    >
      <defs>
        <clipPath id={clipId}
          ><rect x="0" y="0" width={x(shown)} height="132" /></clipPath
        >
      </defs>
      <text x="0" y="34" class="lane">copy engine</text>
      <text x="0" y="82" class="lane">GPU</text>
      <rect x={X0} y="20" width={W} height="22" class="track" />
      <rect x={X0} y="68" width={W} height="22" class="track" />
      <g clip-path={`url(#${clipId})`}>
        {#each run.blocks as b}
          <rect
            x={x(b.start)}
            y="20"
            width={Math.max(
              0.6,
              x(b.end) -
                x(b.start) -
                (b.kind === "stage" && mode === "trickle" ? 0.6 : 0),
            )}
            height="22"
            class={b.kind}
          />
        {/each}
        <rect
          x={x(0)}
          y="68"
          width={x(ARRIVE_MS) - x(0)}
          height="22"
          class="busy"
        />
        <rect
          x={x(ARRIVE_MS)}
          y="68"
          width={Math.max(0.8, x(fetch.end) - x(ARRIVE_MS))}
          height="22"
          class="idle"
        />
        <rect
          x={x(fetch.end)}
          y="68"
          width={x(SPAN) - x(fetch.end)}
          height="22"
          class="busy"
        />
      </g>
      <line x1={x(ARRIVE_MS)} x2={x(ARRIVE_MS)} y1="12" y2="96" class="need" />
      <text x={x(ARRIVE_MS) + 3} y="11" class="tick">needs its experts</text>
      {#each [0, 10, 20, 30, 40, 50] as ms}
        <text x={x(ms)} y="112" class="tick" text-anchor="middle">{ms}</text>
      {/each}
      <text x={X0 + W} y="128" class="tick" text-anchor="end">ms</text>
    </svg>
  </div>

  <div class="key">
    <span class="k stage">next layer's stage-ahead</span>
    <span class="k fetch">copies this layer needs now</span>
    <span class="k idle">GPU idle</span>
  </div>

  <div class="results" aria-label="Measured, warm prefill">
    {#each results as r}
      <div>
        <span class="label">{r.tokens} prefill</span>
        <span class="num">{r.before.toFixed(2)} → {r.after.toFixed(2)} s</span>
      </div>
    {/each}
    <div>
      <span class="label">GPU idle before</span>
      <span class="num">{idleBefore.idle} of {idleBefore.total} s</span>
    </div>
  </div>
</Fig>

<style>
  .plot {
    min-width: 0;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
    font-family: var(--font-code);
  }
  .lane {
    fill: var(--ink-2);
    font-size: 11px;
  }
  .tick {
    fill: var(--ink-2);
    font-size: 10px;
  }
  .track {
    fill: var(--band);
  }
  .stage {
    fill: var(--tone-ram);
  }
  .fetch {
    fill: var(--tone-hot);
  }
  .busy {
    fill: var(--tone-gpu);
  }
  .idle {
    fill: var(--tone-disk);
  }
  .need {
    stroke: var(--ink);
    stroke-width: 1;
    stroke-dasharray: 3 2;
  }
  .key {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1rem;
    margin-top: 0.4rem;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .k::before {
    content: "";
    display: inline-block;
    width: 0.6rem;
    height: 0.6rem;
    margin-right: 0.35rem;
    vertical-align: -0.05rem;
  }
  .k.stage::before {
    background: var(--tone-ram);
  }
  .k.fetch::before {
    background: var(--tone-hot);
  }
  .k.idle::before {
    background: var(--tone-disk);
  }
  .results {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.5rem 1rem;
    margin-top: 0.75rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--line);
  }
  .results div {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }
  .label {
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .num {
    color: var(--ink);
    font: 600 0.85rem var(--font-code);
    font-variant-numeric: tabular-nums;
  }
  @media (max-width: 600px) {
    .results {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
