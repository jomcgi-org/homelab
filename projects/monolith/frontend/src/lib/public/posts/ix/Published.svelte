<script>
  import Fig from "./Fig.svelte";
  import { pct, published, rate, ticks } from "./data-published.js";

  let selected = $state("oom");
  const row = $derived(published.find((r) => r.key === selected));
</script>

<Fig title="Published decode rates on a 96 GB RTX PRO 6000, and the 4090">
  <div class="chart" role="group" aria-label="Decode rate by configuration">
    {#each published as r}
      <button
        type="button"
        class="row"
        class:on={r.key === selected}
        class:ours={r.ours}
        aria-pressed={r.key === selected}
        aria-label={`${r.label}: ${rate(r)}, speculation ${r.speculation ? "on" : "off"}`}
        onclick={() => (selected = r.key)}
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
      </button>
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

  <div class="readout" aria-live="polite">
    <span class="name">{row.label}, {rate(row)}.</span>
    {row.caveat}
    {#if row.url}<a href={row.url} target="_blank" rel="noopener noreferrer"
        >Source</a
      >{/if}
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
    width: 100%;
    min-height: 2.25rem;
    padding: 0;
    border: 0;
    background: none;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
    text-align: left;
    cursor: pointer;
  }
  .row.on,
  .row.ours {
    color: var(--ink);
  }
  .row:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
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
  .row.on .bar {
    border-color: var(--ink);
  }
  .row.on .bar.spec {
    background: var(--ink);
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
  .readout {
    margin-top: 0.6rem;
    color: var(--ink-2);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .name {
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.78rem;
  }
  .readout a {
    color: var(--accent-ink);
    font-family: var(--font-code);
    font-size: 0.75rem;
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
