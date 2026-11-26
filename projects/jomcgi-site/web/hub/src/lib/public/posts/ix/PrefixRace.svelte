<script>
  import Fig from "./Fig.svelte";
  import { runs, REUSED } from "./data-prefix-race.js";

  const end = Math.max(...runs.map((r) => r.seconds));
</script>

<Fig title="First token for a follow-up question on a 32k-token document">
  <div class="race">
    {#each runs as r}
      <div class="lane">
        <span class="name">{r.label}</span>
        <span class="track"
          ><i class={r.key} style={`width:${(r.seconds * 100) / end}%`}
          ></i></span
        >
        <span class="time">{r.seconds} s</span>
        <span class="detail">{r.detail}</span>
      </div>
    {/each}
  </div>

  {#snippet note()}
    {REUSED}. Max-perf config; the 13.1 s is the first question, with nothing to
    reuse (#6859).
  {/snippet}
</Fig>

<style>
  .race {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .lane {
    display: grid;
    grid-template-columns: 8rem minmax(0, 1fr) 3.5rem;
    column-gap: 0.75rem;
    row-gap: 0.15rem;
    align-items: center;
    font: 0.72rem var(--font-code);
    color: var(--ink);
  }
  .track {
    display: block;
    height: 0.75rem;
    background: var(--band);
  }
  .track i {
    display: block;
    height: 100%;
  }
  .prefill {
    background: var(--tone-disk);
  }
  .stored {
    background: var(--tone-ram);
  }
  .live {
    background: var(--tone-gpu);
  }
  .time {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .detail {
    grid-column: 2 / -1;
    color: var(--ink-2);
    font-family: inherit;
    font-size: 0.8rem;
    line-height: 1.4;
  }
  @media (max-width: 600px) {
    .lane {
      grid-template-columns: 6.5rem minmax(0, 1fr) 3rem;
      column-gap: 0.5rem;
    }
    .detail {
      grid-column: 1 / -1;
    }
  }
</style>
