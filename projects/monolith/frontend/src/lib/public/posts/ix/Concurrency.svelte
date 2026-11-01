<script>
  import Fig from "./Fig.svelte";
  import { rows, RATE_MAX } from "./data-concurrency.js";

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
</script>

<Fig title="Requests at once: total output against each request's speed">
  <svg
    bind:clientWidth={rendered}
    style={`--axis-size:${fontSize}px`}
    viewBox={`0 0 ${W} ${H}`}
    role="img"
    aria-label={series
      .map(
        (s) =>
          `${s.label}: ${rows.map((r) => `${r.k} requests ${s.pick(r)} tok/s`).join(", ")}`,
      )
      .join(". ")}
  >
    {#each [0, 20, 40, 60] as v}
      <line class="grid" x1={left} x2={W - right} y1={y(v)} y2={y(v)} />
      <text class="axis" x={left - 8} y={y(v) + 5} text-anchor="end">{v}</text>
    {/each}
    {#each rows as r, i}
      <text class="axis" x={x(i)} y={H - 8} text-anchor="middle">{r.k}</text>
    {/each}
    {#each series as s}
      <polyline class={`series ${s.key}`} points={line(s.pick)} />
      {#each rows as r, i}
        <circle class={`pt ${s.key}`} cx={x(i)} cy={y(s.pick(r))} r="3.5" />
      {/each}
    {/each}
    <text class="axis" x={left} y={top - 2}>tok/s</text>
  </svg>

  <div class="legend">
    {#each series as s}<span class={`key ${s.key}`}>{s.label}</span>{/each}
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
</style>
