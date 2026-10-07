<script>
  import Fig from "./Fig.svelte";
  import { capacities, totalGb, weights, widthPct } from "./data-fit.js";

  const total = totalGb();
  let selected = $state("experts");
  const part = $derived(weights.find((w) => w.key === selected));
</script>

<Fig title="134 GB of weights against 88 GB of memory">
  <div class="rows">
    <div class="row">
      <div class="label">Weights <b>{total} GB</b></div>
      <div class="track" role="group" aria-label="Weights by part">
        {#each weights as w}
          <button
            type="button"
            class={`seg-part ${w.tone}`}
            class:on={w.key === selected}
            style={`width:${widthPct(w.gb)}%`}
            aria-pressed={w.key === selected}
            aria-label={`${w.label}, ${w.gb} GB`}
            onclick={() => (selected = w.key)}
            onmouseenter={() => (selected = w.key)}
            onfocus={() => (selected = w.key)}
          ></button>
        {/each}
      </div>
    </div>
    {#each capacities as c}
      <div class="row">
        <div class="label">{c.label} <b>{c.gb} GB</b></div>
        <div class="track">
          <span class="cap" style={`width:${widthPct(c.gb)}%`}></span>
        </div>
      </div>
    {/each}
  </div>

  <div class="legend">
    {#each weights as w}
      <button
        type="button"
        class={w.tone}
        aria-pressed={w.key === selected}
        onclick={() => (selected = w.key)}><i></i>{w.label} {w.gb} GB</button
      >
    {/each}
  </div>

  <div class="readout" aria-live="polite">
    <span class="name">{part.label}, {part.gb} GB.</span>
    {part.where}
  </div>

</Fig>

<style>
  .rows {
    display: grid;
    gap: 0.6rem;
  }
  .row {
    display: grid;
    grid-template-columns: 9.5rem minmax(0, 1fr);
    gap: 0.75rem;
    align-items: center;
  }
  .label {
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .label b {
    color: var(--ink);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .track {
    display: flex;
    height: 1.6rem;
    min-width: 0;
  }
  .seg-part {
    height: 100%;
    min-width: 0;
    padding: 0;
    border: 0;
    border-right: 1px solid var(--sheet);
    background: var(--t);
    opacity: 0.45;
    cursor: pointer;
  }
  .seg-part.on {
    opacity: 1;
  }
  .seg-part:focus-visible,
  .legend button:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
  .cap {
    height: 100%;
    border: 1.5px solid var(--ink);
    box-sizing: border-box;
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
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 1rem;
    margin-top: 0.75rem;
  }
  .legend button {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    min-height: 2rem;
    padding: 0;
    border: 0;
    background: none;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
    cursor: pointer;
  }
  .legend button[aria-pressed="true"] {
    color: var(--ink);
  }
  .legend i {
    width: 0.65rem;
    height: 0.65rem;
    background: var(--t);
  }
  .readout {
    margin-top: 0.5rem;
    color: var(--ink-2);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .name {
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.78rem;
  }
  @media (max-width: 600px) {
    .row {
      grid-template-columns: 1fr;
      gap: 0.25rem;
    }
  }
</style>
