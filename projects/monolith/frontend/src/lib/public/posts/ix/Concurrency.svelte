<script>
  import Fig from "./Fig.svelte";
  import { rows, RATE_MAX, TTFT_MAX } from "./data-concurrency.js";

  let k = $state(2);
  const row = $derived(rows.find((r) => r.k === k));
  // Chart geometry in viewBox units; K is categorical (1, 2, 4, 8).
  const W = 480;
  const H = 200;
  const left = 44;
  const right = 16;
  const top = 14;
  const bottom = 30;
  const x = (i) => left + (i * (W - left - right)) / (rows.length - 1);
  const y = (v) => top + (1 - v / RATE_MAX) * (H - top - bottom);
  // Axis labels stay about 11 px however wide the chart renders.
  let rendered = $state(0);
  const fontSize = $derived(rendered ? (11 * W) / rendered : 15);
  const line = (pick) => rows.map((r, i) => `${x(i)},${y(pick(r))}`).join(" ");
  const series = [
    { key: "agg", label: "Total, 8 streams", pick: (r) => r.batched.aggregate },
    {
      key: "serial",
      label: "Total, 1 stream",
      pick: (r) => r.serial.aggregate,
    },
    {
      key: "each",
      label: "Each request's decode, 8 streams",
      pick: (r) => r.batched.perStream,
    },
  ];
  const index = $derived(rows.findIndex((r) => r.k === k));
</script>

<Fig title="Requests at once: total output against each request's speed">
  {#snippet controls()}
    <span class="ix-label">Requests</span>
    <div class="seg" role="group" aria-label="Concurrent requests">
      {#each rows as r}
        <button type="button" aria-pressed={k === r.k} onclick={() => (k = r.k)}
          >{r.k}</button
        >
      {/each}
    </div>
  {/snippet}

  <svg
    bind:clientWidth={rendered}
    style={`--axis-size:${fontSize}px`}
    viewBox={`0 0 ${W} ${H}`}
    role="img"
    aria-label={`${k} requests: ${row.batched.aggregate} tok/s total with 8 streams, ${row.batched.perStream} tok/s each; ${row.serial.aggregate} tok/s total with 1 stream.`}
  >
    {#each [0, 20, 40, 60] as v}
      <line class="grid" x1={left} x2={W - right} y1={y(v)} y2={y(v)} />
      <text class="axis" x={left - 8} y={y(v) + 5} text-anchor="end">{v}</text>
    {/each}
    {#each rows as r, i}
      <text
        class="axis"
        class:on={r.k === k}
        x={x(i)}
        y={H - 8}
        text-anchor="middle">{r.k}</text
      >
    {/each}
    <line class="cursor" x1={x(index)} x2={x(index)} y1={top} y2={H - bottom} />
    {#each series as s}
      <polyline class={`series ${s.key}`} points={line(s.pick)} />
      {#each rows as r, i}
        <circle
          class={`pt ${s.key}`}
          class:on={r.k === k}
          cx={x(i)}
          cy={y(s.pick(r))}
          r={r.k === k ? 5 : 3}
        />
      {/each}
    {/each}
    <text class="axis" x={left} y={top - 2}>tok/s</text>
  </svg>

  <div class="legend">
    {#each series as s}<span class={`key ${s.key}`}>{s.label}</span>{/each}
  </div>

  <div class="readout" aria-live="polite">
    <div class="figs">
      <span><b>{row.batched.aggregate}</b> tok/s total</span>
      <span><b>{row.batched.perStream}</b> tok/s each</span>
      <span>First token <b>{row.batched.ttftP50} s</b> median</span>
    </div>
    <div class="ttft">
      {#each [["8 streams", row.batched], ["1 stream", row.serial]] as [label, r]}
        <div class="tt">
          <span class="tl">{label}</span>
          <span class="track"
            ><i class="p50" style={`width:${(r.ttftP50 / TTFT_MAX) * 100}%`}
            ></i><i
              class="max"
              style={`width:${((r.ttftMax - r.ttftP50) / TTFT_MAX) * 100}%`}
            ></i></span
          >
          <span class="tv">{r.ttftP50} / {r.ttftMax} s</span>
        </div>
      {/each}
      <div class="tcap">First token, median / worst</div>
    </div>
  </div>

  {#snippet note()}
    128-token requests, temperature 0, median of two rounds. Two runs at 4
    requests that schedule identically differed by 15% (54.9 against 47.8
    tok/s). The server defaults to 2 streams.
  {/snippet}
</Fig>

<style>
  svg {
    display: block;
    width: 100%;
    max-width: 32rem;
    height: auto;
    font-family: var(--font-code);
  }
  .grid {
    stroke: var(--line);
  }
  .axis {
    fill: var(--ink-2);
    font-size: var(--axis-size);
  }
  .axis.on {
    fill: var(--ink);
    font-weight: 600;
  }
  .cursor {
    stroke: var(--ink-3);
    stroke-dasharray: 3 3;
  }
  .series {
    fill: none;
    stroke-width: 2.5;
  }
  .series.agg,
  .pt.agg {
    stroke: var(--tone-gpu);
    fill: var(--tone-gpu);
  }
  .series.serial,
  .pt.serial {
    stroke: var(--ink-3);
    fill: var(--ink-3);
  }
  .series.serial {
    fill: none;
    stroke-dasharray: 5 4;
  }
  .series.each,
  .pt.each {
    stroke: var(--tone-ram);
    fill: var(--tone-ram);
  }
  .series.agg,
  .series.each {
    fill: none;
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1rem;
    margin-top: 0.4rem;
    color: var(--ink-2);
    font: 0.68rem var(--font-code);
  }
  .key::before {
    content: "";
    display: inline-block;
    width: 1rem;
    height: 3px;
    margin-right: 0.35rem;
    vertical-align: 0.2rem;
  }
  .key.agg::before {
    background: var(--tone-gpu);
  }
  .key.serial::before {
    background: repeating-linear-gradient(
      90deg,
      var(--ink-3) 0 4px,
      transparent 4px 7px
    );
  }
  .key.each::before {
    background: var(--tone-ram);
  }
  .readout {
    margin-top: 0.85rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--line);
  }
  .figs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1.25rem;
    color: var(--ink-2);
    font: 0.75rem var(--font-code);
  }
  .figs b {
    color: var(--ink);
    font-weight: 600;
  }
  .ttft {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    margin-top: 0.7rem;
  }
  .tt {
    display: grid;
    grid-template-columns: 6rem minmax(0, 1fr) 6.5rem;
    gap: 0.6rem;
    align-items: center;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .track {
    display: flex;
    height: 0.6rem;
    background: var(--band);
  }
  .track i {
    display: block;
    height: 100%;
  }
  .p50 {
    background: var(--ink);
  }
  .max {
    background: var(--ink-3);
  }
  .tv {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .tcap {
    color: var(--ink-3);
    font: 0.66rem var(--font-code);
  }
  @media (max-width: 600px) {
    .tt {
      grid-template-columns: 4.5rem minmax(0, 1fr) 5.5rem;
      gap: 0.4rem;
    }
  }
</style>
