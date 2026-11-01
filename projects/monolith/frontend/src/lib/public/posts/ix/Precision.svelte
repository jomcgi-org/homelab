<script>
  import Fig from "./Fig.svelte";
  import { settings, KL_SCALE } from "./data-precision.js";

  const SPEED_SCALE = 25;
  const klText = (kl) =>
    kl[0] === kl[1]
      ? `KL ${kl[0]}`
      : `KL ${kl[0].toFixed(2)}–${kl[1].toFixed(2)}`;
</script>

<Fig title="What each precision setting buys, and what it costs in accuracy">
  <div class="grid">
    <span class="head"></span>
    <span class="head">Faster</span>
    <span class="head">Drift from exact</span>
    {#each settings as x}
      <div class="row" class:default={x.status === "Default"}>
        <span class="name"
          ><b>{x.label}</b><code>{x.flag}</code><span class="status"
            >{x.status}</span
          ></span
        >
        <span class="cell">
          <span class="bar"
            ><i
              class="speed"
              style={`width:${(x.gainPercent / SPEED_SCALE) * 100}%`}
            ></i></span
          >
          <span class="val">{x.gainPercent}% {x.gainPhase}</span>
          <span class="detail">{x.gain}</span>
          {#if x.slots}<span class="detail">{x.slots}</span>{/if}
        </span>
        <span class="cell">
          <span class="bar">
            {#if x.kl}<i
                class="drift"
                style={`width:${(x.kl[1] / KL_SCALE) * 100}%`}
              ></i>{:else}<i class="drift noise"></i>{/if}
          </span>
          <span class="val"
            >{x.kl
              ? `${klText(x.kl)}, ${x.top1} top-1`
              : "rounding noise"}</span
          >
          <span class="detail">{x.accuracy}</span>
        </span>
      </div>
    {/each}
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
    align-items: start;
    padding: 0.6rem 0;
    border-top: 1px solid var(--line);
    color: var(--ink);
  }
  .name {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }
  .name b {
    font-weight: 600;
  }
  .name code {
    padding: 0;
    background: none;
    color: var(--ink-2);
    font: inherit;
    overflow-wrap: anywhere;
  }
  .status {
    color: var(--ink-3);
  }
  .default .status {
    color: var(--ink);
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
    margin-top: 0.2rem;
    background: var(--band);
  }
  .bar i {
    display: block;
    height: 100%;
  }
  .speed {
    background: var(--tone-gpu);
  }
  .drift {
    background: var(--tone-disk);
  }
  .drift.noise {
    width: 4%;
    background: repeating-linear-gradient(
      135deg,
      var(--tone-disk) 0 2px,
      transparent 2px 4px
    );
  }
  .val {
    overflow-wrap: anywhere;
  }
  .detail {
    color: var(--ink-2);
    font: 0.78rem / 1.4 var(--font-ui);
  }
  @media (max-width: 600px) {
    .grid {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      column-gap: 0.5rem;
    }
    .head:first-child {
      display: none;
    }
    .name {
      grid-column: 1 / -1;
      margin-bottom: 0.35rem;
    }
  }
</style>
