<script>
  import Fig from "./Fig.svelte";
  import { pct, published, rate, ticks } from "./data-published.js";
</script>

<Fig title="Published decode rates on a 96 GB RTX PRO 6000, and the 4090">
  <div class="chart" role="list" aria-label="Decode rate by configuration">
    {#each published as r}
      <div
        class="row"
        class:ours={r.ours}
        role="listitem"
        aria-label={`${r.label}: ${rate(r)}, speculation ${r.speculation ? "on" : "off"}`}
      >
        <span class="label">{r.label}</span>
        <span class="track">
          <span
            class="bar"
            class:spec={r.speculation}
            style={`width:${pct(r.from)}%`}
          ></span>
          {#if r.to > r.from}<span
              class="range"
              style={`left:${pct(r.from)}%;width:${pct(r.to - r.from)}%`}
            ></span>{/if}
        </span>
        <span class="value">{rate(r)}</span>
      </div>
    {/each}
    <div class="axis" aria-hidden="true">
      <span></span>
      <span class="ticks">
        {#each ticks as t}<span style={`left:${pct(t)}%`}>{t}</span>{/each}
      </span>
      <span></span>
    </div>
  </div>

  <div class="key" aria-hidden="true">
    <span><i class="spec"></i>Speculation on</span>
    <span><i></i>Speculation off</span>
  </div>

  {#snippet note()}
    Author-reported scale references: different checkpoints, precisions and
    workloads. Not a controlled ranking.
  {/snippet}
</Fig>

<style>
  .chart {
    display: grid;
    gap: 0.15rem;
  }
  .row,
  .axis {
    display: grid;
    grid-template-columns: 11rem minmax(0, 1fr) 7.5rem;
    gap: 0.75rem;
    align-items: center;
  }
  .row {
    min-height: 2.25rem;
    color: var(--ink);
    font: 0.7rem var(--font-code);
  }
  .track {
    position: relative;
    height: 0.9rem;
    min-width: 0;
  }
  .bar,
  .range {
    position: absolute;
    top: 0;
    height: 100%;
    box-sizing: border-box;
  }
  .bar {
    left: 0;
    border: 1.5px solid var(--ink-3);
  }
  .bar.spec {
    border: 0;
    background: var(--ink-3);
  }
  .range {
    background: repeating-linear-gradient(
      135deg,
      var(--ink-3) 0 3px,
      transparent 3px 6px
    );
  }
  .row.ours .bar.spec {
    background: var(--tone-hot);
  }
  .value {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .axis {
    margin-top: 0.15rem;
    color: var(--ink-3);
    font: 0.65rem var(--font-code);
  }
  .ticks {
    position: relative;
    height: 1rem;
    border-top: 1px solid var(--line);
  }
  .ticks span {
    position: absolute;
    top: 0.15rem;
    transform: translateX(-50%);
  }
  .ticks span:first-child {
    transform: none;
  }
  .ticks span:last-child {
    transform: translateX(-100%);
  }
  .key {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 1rem;
    margin-top: 0.6rem;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .key span {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .key i {
    width: 0.9rem;
    height: 0.6rem;
    border: 1.5px solid var(--ink-3);
    box-sizing: border-box;
  }
  .key i.spec {
    border: 0;
    background: var(--ink-3);
  }
  @media (max-width: 600px) {
    .row,
    .axis {
      grid-template-columns: minmax(0, 1fr) 6.5rem;
      grid-template-areas: "label value" "track track";
      gap: 0.2rem 0.5rem;
    }
    .row {
      padding-block: 0.3rem;
    }
    .label {
      grid-area: label;
    }
    .track,
    .ticks {
      grid-area: track;
    }
    .value {
      grid-area: value;
    }
    .axis > span:not(.ticks) {
      display: none;
    }
  }
</style>
