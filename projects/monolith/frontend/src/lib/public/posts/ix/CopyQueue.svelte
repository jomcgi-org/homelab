<script>
  import Fig from "./Fig.svelte";
  import {
    ARRIVE_MS,
    PIECE_RECORDS,
    STAGE_MS,
    STAGE_RECORDS,
    idleBefore,
    results,
    simulate,
  } from "./data-copy-queue.js";

  const SPAN = 50; // ms shown
  const X0 = 96;
  // Drawn at 1:1 pixels once the page measures it; 640 until then.
  let width = $state(640);
  const VBW = $derived(Math.max(300, Math.round(width)));
  const W = $derived(VBW - X0 - 8);
  const x = (ms) => X0 + (Math.min(ms, SPAN) / SPAN) * W;
  const waitLabel = (wait) =>
    wait >= 1 ? `waits ${Math.round(wait)} ms` : "waits under 1 ms";

  // The two ways to submit a layer's stage-ahead, one above the other.
  const panels = [
    { mode: "all", title: "All at once", top: 18 },
    { mode: "trickle", title: `${PIECE_RECORDS} at a time`, top: 118 },
  ].map((p) => {
    const run = simulate(p.mode);
    return { ...p, run, fetch: run.blocks.find((b) => b.kind === "fetch") };
  });
</script>

<Fig title="Prefetch in small batches so urgent copies don't wait">
  <div class="plot" bind:clientWidth={width}>
    <svg
      viewBox={`0 0 ${VBW} 232`}
      role="img"
      aria-label={panels
        .map((p) => `${p.title}: this layer's copies ${waitLabel(p.run.wait)}`)
        .join("; ")}
    >
      {#each panels as p}
        <text x="0" y={p.top} class="title"
          >{p.title}
          <tspan class="wait">· this layer {waitLabel(p.run.wait)}</tspan></text
        >
        <text x="0" y={p.top + 24} class="lane">copy engine</text>
        <text x="0" y={p.top + 56} class="lane">GPU</text>
        <rect x={X0} y={p.top + 10} width={W} height="20" class="track" />
        {#each p.run.blocks as b}
          <rect
            x={x(b.start)}
            y={p.top + 10}
            width={Math.max(
              0.8,
              x(b.end) -
                x(b.start) -
                (b.kind === "stage" && p.mode === "trickle" ? 0.8 : 0),
            )}
            height="20"
            class={b.kind}
          />
        {/each}
        <rect
          x={x(0)}
          y={p.top + 42}
          width={x(ARRIVE_MS) - x(0)}
          height="20"
          class="busy"
        />
        <rect
          x={x(ARRIVE_MS)}
          y={p.top + 42}
          width={Math.max(0.8, x(p.fetch.end) - x(ARRIVE_MS))}
          height="20"
          class="idle"
        />
        <rect
          x={x(p.fetch.end)}
          y={p.top + 42}
          width={x(SPAN) - x(p.fetch.end)}
          height="20"
          class="busy"
        />
        <line
          x1={x(ARRIVE_MS)}
          x2={x(ARRIVE_MS)}
          y1={p.top + 6}
          y2={p.top + 66}
          class="need"
        />
      {/each}
      {#each [0, 10, 20, 30, 40, 50] as ms}
        <text x={x(ms)} y="212" class="tick" text-anchor="middle">{ms}</text>
      {/each}
      <text x={X0 + W} y="228" class="tick" text-anchor="end">ms</text>
    </svg>
  </div>

  <div class="key">
    <span class="k stage">next layer's stage-ahead</span>
    <span class="k fetch">copies this layer needs now</span>
    <span class="k busy">GPU computing</span>
    <span class="k idle">GPU idle</span>
    <span class="k need">this layer needs its experts</span>
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

  {#snippet note()}
    A layer's stage-ahead is about {STAGE_RECORDS} records, 1.2 GB and {STAGE_MS}
    ms of copy-engine time. Timelines are schematic; prefill times are measured, warm.
  {/snippet}
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
  .title {
    fill: var(--ink);
    font-size: 13px;
    font-weight: 600;
  }
  .wait {
    fill: var(--ink-2);
    font-weight: 400;
  }
  .lane {
    fill: var(--ink-2);
    font-size: 12px;
  }
  .tick {
    fill: var(--ink-2);
    font-size: 11px;
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
  .k.busy::before {
    background: var(--tone-gpu);
  }
  .k.idle::before {
    background: var(--tone-disk);
  }
  .k.need::before {
    width: 0;
    height: 0.75rem;
    border-left: 1px dashed var(--ink);
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
