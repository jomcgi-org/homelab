<script>
  import Fig from "./Fig.svelte";
  import { runs, REUSED, SPEED, progress } from "./data-prefix-race.js";

  const end = Math.max(...runs.map((r) => r.seconds));
  // Rest on the finished race; "Ask a follow-up" replays it.
  let t = $state(end);
  let playing = $state(false);
  let frame = 0;

  function play() {
    cancelAnimationFrame(frame);
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
      t = end;
      return;
    }
    playing = true;
    t = 0;
    const start = performance.now();
    const step = (now) => {
      t = Math.min(end, ((now - start) / 1000) * SPEED);
      if (t < end) frame = requestAnimationFrame(step);
      else playing = false;
    };
    frame = requestAnimationFrame(step);
  }
  // Effects run only in the browser, so this never touches the server.
  $effect(() => () => cancelAnimationFrame(frame));
  const done = $derived(runs.filter((r) => t >= r.seconds));
</script>

<Fig title="First token for a follow-up question on a 32k-token document">
  {#snippet controls()}
    <div class="seg">
      <button type="button" onclick={play} disabled={playing}
        >Ask a follow-up</button
      >
    </div>
    <span class="ix-label clock" aria-hidden="true"
      >{t.toFixed(1)} s{playing ? ` · ${SPEED}x speed` : ""}</span
    >
  {/snippet}

  <div class="race">
    {#each runs as r}
      <div class="lane" class:done={t >= r.seconds}>
        <span class="name">{r.label}</span>
        <span class="track"
          ><i
            class={r.key}
            style={`width:${(progress(r, t) * r.seconds * 100) / end}%`}
          ></i></span
        >
        <span class="time">{t >= r.seconds ? `${r.seconds} s` : ""}</span>
        <span class="detail">{r.detail}</span>
      </div>
    {/each}
  </div>
  <div class="sr" aria-live="polite">
    {#if !playing}{runs
        .map((r) => `${r.label}: ${r.seconds} s`)
        .join("; ")}{:else if done.length}{done.at(-1).label} first token at {done.at(
        -1,
      ).seconds} s{/if}
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
    color: var(--ink-2);
  }
  .lane.done {
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
  .clock {
    font-variant-numeric: tabular-nums;
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .seg button:disabled {
    cursor: default;
    opacity: 0.6;
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
