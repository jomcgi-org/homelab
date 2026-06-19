<script>
  // Per-day navigation header: back to the trip summary, prev/next day, and the
  // day label + colour swatch.
  let { slug, dayNumber, totalDays, label, dayColor = "var(--ink)" } = $props();

  const hasPrev = $derived(dayNumber > 1);
  const hasNext = $derived(dayNumber < totalDays);
</script>

<nav class="daynav" style={`--day:${dayColor}`} aria-label="Day navigation">
  <a class="back" href={`/app/trips/${slug}`}>&larr; Summary</a>

  <div class="title">
    <span class="swatch" aria-hidden="true"></span>
    <span class="label">{label}</span>
    <span class="count">Day {dayNumber} / {totalDays}</span>
  </div>

  <div class="arrows">
    {#if hasPrev}
      <a class="arrow" href={`/app/trips/${slug}/day/${dayNumber - 1}`} aria-label="Previous day">&larr;</a>
    {:else}
      <span class="arrow disabled" aria-hidden="true">&larr;</span>
    {/if}
    {#if hasNext}
      <a class="arrow" href={`/app/trips/${slug}/day/${dayNumber + 1}`} aria-label="Next day">&rarr;</a>
    {:else}
      <span class="arrow disabled" aria-hidden="true">&rarr;</span>
    {/if}
  </div>
</nav>

<style>
  .daynav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px 20px;
    flex-wrap: wrap;
    padding-bottom: 14px;
    margin-bottom: 20px;
    border-bottom: 2px solid var(--ink);
  }
  .back {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink);
    text-decoration: none;
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 8px 12px;
    transition:
      transform 110ms ease,
      box-shadow 110ms ease;
  }
  .back:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }
  .title {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .swatch {
    width: 14px;
    height: 14px;
    background: var(--day);
    border: 2px solid var(--ink);
    flex: none;
  }
  .label {
    font-family: var(--mono);
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--ink);
  }
  .count {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    text-transform: uppercase;
  }
  .arrows {
    display: inline-flex;
  }
  .arrow {
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 700;
    color: var(--ink);
    text-decoration: none;
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 6px 14px;
  }
  .arrow + .arrow {
    border-left: none;
  }
  .arrow.disabled {
    opacity: 0.3;
  }
  a.arrow:hover {
    background: var(--ink);
    color: var(--paper);
  }
</style>
