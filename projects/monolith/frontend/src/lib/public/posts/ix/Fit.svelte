<script>
  import Fig from "./Fig.svelte";
  import { capacities, totalGb, weights, widthPct } from "./data-fit.js";

  const total = totalGb();
  // VRAM alone, and VRAM plus host RAM, marked on the weights bar itself.
  const marks = capacities.filter((c) => c.key !== "ram");
</script>

<Fig title="134 GB of weights against 88 GB of memory">
  <div class="chart">
    <div
      class="bar"
      role="img"
      aria-label={`${total} GB of weights: ${weights.map((w) => `${w.label} ${w.gb} GB`).join(", ")}. VRAM holds 24 GB; VRAM and host RAM together hold 88 GB.`}
    >
      {#each weights as w}
        <span class={`part ${w.tone}`} style={`width:${widthPct(w.gb)}%`}
        ></span>
      {/each}
      {#each marks as c}
        <span class="mark" style={`left:${widthPct(c.gb)}%`}>
          <span class="mark-label"
            >{c.key === "vram" ? "VRAM" : "+ RAM"} {c.gb} GB</span
          >
        </span>
      {/each}
    </div>
    <div class="scale" aria-hidden="true">
      <span>0</span><span>{total} GB</span>
    </div>
  </div>

  <div class="key">
    {#each weights as w}
      <div class={`key-row ${w.tone}`}>
        <i></i>
        <span class="name">{w.label} <b>{w.gb} GB</b></span>
        <span class="where">{w.where}</span>
      </div>
    {/each}
  </div>
</Fig>

<style>
  .chart {
    padding-top: 1.4rem;
  }
  .bar {
    position: relative;
    display: flex;
    height: 1.8rem;
  }
  .part {
    height: 100%;
    border-right: 1px solid var(--sheet);
    background: var(--t);
  }
  .mark {
    position: absolute;
    top: -1.4rem;
    bottom: -0.3rem;
    border-left: 1.5px solid var(--ink);
  }
  .mark-label {
    position: absolute;
    top: 0;
    left: 0.35rem;
    white-space: nowrap;
    color: var(--ink);
    font: 0.68rem var(--font-code);
  }
  .scale {
    display: flex;
    justify-content: space-between;
    margin-top: 0.4rem;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .gpu {
    --t: var(--tone-gpu);
  }
  .disk {
    --t: var(--tone-disk);
  }
  .ram {
    --t: var(--tone-ram);
  }
  .cache {
    --t: var(--tone-cache);
  }
  .key {
    display: grid;
    gap: 0.55rem;
    margin-top: 1rem;
  }
  .key-row {
    display: grid;
    grid-template-columns: 0.65rem 12rem minmax(0, 1fr);
    gap: 0.6rem;
    align-items: baseline;
  }
  .key-row i {
    width: 0.65rem;
    height: 0.65rem;
    background: var(--t);
  }
  .name {
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .name b {
    color: var(--ink);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .where {
    color: var(--ink-2);
    font-size: 0.82rem;
    line-height: 1.45;
  }
  @media (max-width: 600px) {
    .key-row {
      grid-template-columns: 0.65rem minmax(0, 1fr);
    }
    .where {
      grid-column: 2;
    }
  }
</style>
