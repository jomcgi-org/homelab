<script>
  // Per-day navigation header, a faithful port of the original React
  // DayNavigation: Summary link on the left, the day eyebrow + colour-underlined
  // title centred, prev/next day buttons on the right. Buttons invert on hover;
  // unavailable prev/next render as dimmed, non-interactive spans. `date` is the
  // already-formatted day date (e.g. "Sep 15, 2024").
  let {
    slug,
    dayNumber,
    totalDays,
    label,
    dayColor = "#1a1a1a",
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
    <div class="label" style={`border-bottom:3px solid ${dayColor}`}>
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
    justify-content: space-between;
    align-items: center;
    gap: 20px;
    padding: 20px 0;
    border-bottom: 3px solid #1a1a1a;
    margin-bottom: 24px;
  }

  /* Neo-brutalist nav buttons: chunky ink border + hard offset drop-shadow. */
  .navbtn {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 8px 14px;
    font-family:
      system-ui,
      -apple-system,
      sans-serif;
    font-size: 13px;
    font-weight: 600;
    color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    background: white;
    border: 3px solid #1a1a1a;
    box-shadow: 3px 3px 0 0 #1a1a1a;
    text-decoration: none;
    cursor: pointer;
    transition:
      background 0.15s,
      color 0.15s,
      box-shadow 0.1s,
      transform 0.1s;
  }
  a.navbtn:hover {
    background: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    color: white;
  }
  /* Tactile "press": the button shifts onto its shadow. */
  a.navbtn:active {
    transform: translate(2px, 2px);
    box-shadow: 1px 1px 0 0 #1a1a1a;
  }
  .navbtn.disabled {
    opacity: 0.4;
    cursor: not-allowed;
    pointer-events: none;
    box-shadow: none;
  }
  .chev {
    width: 16px;
    height: 16px;
    flex: none;
  }

  .title {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    flex: 1;
    text-align: center;
    min-width: 0;
  }
  .eyebrow {
    font-family: monospace;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #9ca3af; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .date {
    margin-left: 8px;
  }
  .label {
    font-family:
      system-ui,
      -apple-system,
      sans-serif;
    font-size: 18px;
    font-weight: 700;
    color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    padding-bottom: 4px;
    letter-spacing: 0.02em;
  }

  .arrows {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }

  @media (max-width: 768px) {
    .daynav {
      flex-direction: column;
      align-items: stretch;
      gap: 16px;
      padding: 16px 0;
      margin-bottom: 16px;
    }
    .arrows {
      justify-content: center;
    }
  }
</style>
