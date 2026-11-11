<script>
  // Summary and adjacent-day links retain their existing URLs. Unavailable
  // neighbours remain non-interactive spans. The page supplies the zoned date.
  let {
    slug,
    dayNumber,
    totalDays,
    label,
    dayColor = "var(--ink)",
    date = "",
  } = $props();

  const hasPrev = $derived(dayNumber > 1);
  const hasNext = $derived(dayNumber < totalDays);
</script>

<nav class="daynav" aria-label="Day navigation">
  <a class="navbtn" href={`/app/trips/${slug}`}>
    <svg
      class="chev"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"><polyline points="15 18 9 12 15 6" /></svg
    >
    <span>Summary</span>
  </a>

  <div class="title">
    <div class="eyebrow">
      Day {dayNumber} of {totalDays}{#if date}<span class="date">{date}</span
        >{/if}
    </div>
    <div class="label" style={`border-bottom:1px solid ${dayColor}`}>
      {label}
    </div>
  </div>

  <div class="arrows">
    {#if hasPrev}
      <a class="navbtn" href={`/app/trips/${slug}/day/${dayNumber - 1}`}>
        <svg
          class="chev"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"><polyline points="15 18 9 12 15 6" /></svg
        >
        <span>Prev</span>
      </a>
    {:else}
      <span class="navbtn disabled" aria-hidden="true">
        <svg
          class="chev"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg
        >
        <span>Prev</span>
      </span>
    {/if}

    {#if hasNext}
      <a class="navbtn" href={`/app/trips/${slug}/day/${dayNumber + 1}`}>
        <span>Next</span>
        <svg
          class="chev"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"><polyline points="9 18 15 12 9 6" /></svg
        >
      </a>
    {:else}
      <span class="navbtn disabled" aria-hidden="true">
        <span>Next</span>
        <svg
          class="chev"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"><polyline points="9 18 15 12 9 6" /></svg
        >
      </span>
    {/if}
  </div>
</nav>

<style>
  .daynav {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 1rem;
    padding: 1rem;
    border: 1px solid var(--ink);
    margin-bottom: 1.5rem;
    background: var(--band);
  }
  .navbtn {
    display: flex;
    align-items: center;
    gap: 4px;
    min-height: 44px;
    padding: 0.5rem;
    font: 0.8rem var(--font-code);
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    text-decoration: none;
  }
  a.navbtn:hover {
    color: var(--accent-ink);
  }
  .navbtn.disabled {
    color: var(--ink-2);
    cursor: not-allowed;
  }
  .chev {
    width: 16px;
    height: 16px;
    flex: none;
  }
  .title {
    flex: 1;
    min-width: min(100%, 12rem);
  }
  .eyebrow {
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
  }
  .date {
    margin-left: 0.5rem;
  }
  .label {
    font: 700 1.2rem var(--font-ui);
    color: var(--ink);
    padding-bottom: 4px;
  }
  .arrows {
    display: flex;
    gap: 0.5rem;
  }
</style>
