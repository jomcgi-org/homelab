<script>
  import Fig from "./Fig.svelte";
  import { settings, KL_SCALE } from "./data-precision.js";

  let selected = $state("k8v6");
  const s = $derived(settings.find((x) => x.key === selected));
  const SPEED_SCALE = 25;
  const klText = (kl) =>
    kl === null
      ? "no KL given"
      : kl[0] === kl[1]
        ? `KL ${kl[0]}`
        : `KL ${kl[0].toFixed(2)}–${kl[1].toFixed(2)}`;
</script>

<Fig title="What each precision setting buys, and what it costs in accuracy">
  {#snippet controls()}
    <div class="seg" role="group" aria-label="Precision setting">
      {#each settings as x}
        <button
          type="button"
          aria-pressed={selected === x.key}
          onclick={() => (selected = x.key)}>{x.label}</button
        >
      {/each}
    </div>
  {/snippet}

  <div class="grid">
    <span class="head"></span>
    <span class="head">Faster</span>
    <span class="head">Drift from exact</span>
    {#each settings as x}
      <div class="row" class:on={x.key === selected}>
        <span class="name">{x.label}<span class="status">{x.status}</span></span
        >
        <span class="cell">
          <span class="bar"
            ><i
              class="speed"
              style={`width:${(x.gainPercent / SPEED_SCALE) * 100}%`}
            ></i></span
          >
          <span class="val">{x.gainPercent}% {x.gainPhase}</span>
        </span>
        <span class="cell">
          <span class="bar">
            {#if x.kl}<i
                class="drift"
                style={`width:${(x.kl[1] / KL_SCALE) * 100}%`}
              ></i>{:else}<i class="drift noise"></i>{/if}
          </span>
          <span class="val">{x.kl ? klText(x.kl) : "rounding noise"}</span>
        </span>
      </div>
    {/each}
  </div>

  <div class="readout" aria-live="polite">
    <div class="flag">{s.flag} · {s.status}</div>
    <div class="changes">{s.changes}</div>
    <div class="facts">
      <span>{s.gain}</span>
      {#if s.slots}<span>{s.slots}</span>{/if}
      <span
        >{s.accuracy}{#if s.kl}; {klText(s.kl)}{/if}{#if s.top1}, {s.top1} top-1{/if}</span
      >
    </div>
  </div>

  {#snippet note()}
    Each setting measured on its own against the exact reference; the effects do
    not add. The demos used all four. Faster: share of that phase's time saved
    (decode rate gained for k8v6).
  {/snippet}
</Fig>

<style>
  .grid {
    display: grid;
    grid-template-columns: minmax(9rem, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
    column-gap: 0.75rem;
    font: 0.7rem var(--font-code);
  }
  .head {
    padding-bottom: 0.35rem;
    color: var(--ink-2);
  }
  .row {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: center;
    min-height: 2.75rem;
    padding: 0.3rem 0;
    border-top: 1px solid var(--line);
    color: var(--ink-2);
  }
  .row.on {
    color: var(--ink);
  }
  .name {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  .status {
    color: var(--ink-3);
  }
  .row.on .name {
    font-weight: 600;
  }
  .cell {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 0;
  }
  .bar {
    display: block;
    height: 0.55rem;
    background: var(--band);
  }
  .bar i {
    display: block;
    height: 100%;
    background: var(--ink-3);
  }
  .row.on .speed {
    background: var(--tone-gpu);
  }
  .row.on .drift {
    background: var(--tone-disk);
  }
  .drift.noise {
    width: 4%;
    background: repeating-linear-gradient(
      135deg,
      var(--ink-3) 0 2px,
      transparent 2px 4px
    );
  }
  .row.on .drift.noise {
    background: repeating-linear-gradient(
      135deg,
      var(--tone-disk) 0 2px,
      transparent 2px 4px
    );
  }
  .val {
    overflow-wrap: anywhere;
  }
  .readout {
    margin-top: 0.85rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--line);
  }
  .flag {
    color: var(--ink);
    font: 600 0.8rem var(--font-code);
  }
  .changes {
    margin-top: 0.35rem;
    color: var(--ink);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .facts {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin-top: 0.4rem;
    color: var(--ink-2);
    font: 0.72rem / 1.45 var(--font-code);
  }
  @media (max-width: 600px) {
    .grid {
      grid-template-columns: minmax(6.5rem, 1fr) minmax(0, 1fr) minmax(0, 1fr);
      column-gap: 0.5rem;
    }
  }
</style>
