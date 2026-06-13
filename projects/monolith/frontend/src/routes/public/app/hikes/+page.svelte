<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import HikesMap from "$lib/public/components/hikes/HikesMap.svelte";
  import {
    filterWalksByCharacteristics,
    groupWindowsByDay,
    upcomingUkDays,
    viableInNextDays,
  } from "$lib/public/hikes/filters.js";

  let { data } = $props();

  let walks = $derived(data.snapshot?.walks ?? []);

  // The five numeric filters, ported from the old sidebar. Empty means "no
  // constraint" (the filters module reads undefined/NaN as unbounded).
  let minDuration = $state("");
  let maxDuration = $state("");
  let minDistance = $state("");
  let maxDistance = $state("");
  let maxAscent = $state("");

  // Date strip: one chip per upcoming UK-local day. null means "any day".
  const DATE_HORIZON = 7;
  let selectedDay = $state(null);

  function numOr(value, fallback) {
    const n = parseFloat(value);
    return Number.isNaN(n) ? fallback : n;
  }

  let dayKeys = $derived(upcomingUkDays(DATE_HORIZON));

  function dayLabel(key) {
    // Noon UTC keeps the label on the intended UK calendar day across BST/GMT.
    return new Date(`${key}T12:00:00Z`).toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      timeZone: "Europe/London",
    });
  }

  // Walks passing the numeric filters.
  let byCharacteristics = $derived(
    filterWalksByCharacteristics(walks, {
      minDuration: numOr(minDuration, -Infinity),
      maxDuration: numOr(maxDuration, Infinity),
      minDistance: numOr(minDistance, -Infinity),
      maxDistance: numOr(maxDistance, Infinity),
      maxAscent: numOr(maxAscent, Infinity),
    }),
  );

  // Then by the selected day (a walk qualifies if it has any window on that UK
  // day). With no day selected, the characteristic set passes through.
  let filtered = $derived(
    selectedDay == null
      ? byCharacteristics
      : byCharacteristics.filter((w) => {
          const byDay = groupWindowsByDay(w);
          return (byDay[selectedDay]?.length ?? 0) > 0;
        }),
  );

  let viableTodayCount = $derived(
    walks.filter((w) => viableInNextDays(w, 1)).length,
  );

  function resetFilters() {
    minDuration = "";
    maxDuration = "";
    minDistance = "";
    maxDistance = "";
    maxAscent = "";
    selectedDay = null;
  }

  onMount(() => {
    // Live updates: re-run the SSR load on a 30 min timer (windows only change
    // 6-hourly, so this is plenty). No client-side call to /api/hikes/* happens.
    const refresh = setInterval(() => invalidateAll(), 30 * 60_000);
    return () => clearInterval(refresh);
  });
</script>

<svelte:head>
  <title>Hike planner, Scotland walks by weather window</title>
  <meta
    name="description"
    content="A map of Scottish hill walks filtered by distance, ascent, duration, and viable weather windows from the met.no forecast."
  />
</svelte:head>

<div class="hikes-page">
  <h1 class="sr-only">Hike planner, Scotland walks by weather window</h1>

  <header class="topbar">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="https://jomcgi.dev/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">hikes</span>
    </nav>
    <p class="topbar-stats">
      {walks.length} walks &middot; {viableTodayCount} viable today &middot;
      {filtered.length} shown
    </p>
  </header>

  <div class="layout">
    <aside class="sidebar">
      <p class="eyebrow">Filters</p>

      <div class="filter-grid">
        <label
          >Min duration (h)
          <input type="number" min="0" step="0.5" bind:value={minDuration} />
        </label>
        <label
          >Max duration (h)
          <input type="number" min="0" step="0.5" bind:value={maxDuration} />
        </label>
        <label
          >Min distance (km)
          <input type="number" min="0" step="1" bind:value={minDistance} />
        </label>
        <label
          >Max distance (km)
          <input type="number" min="0" step="1" bind:value={maxDistance} />
        </label>
        <label class="filter-wide"
          >Max ascent (m)
          <input type="number" min="0" step="50" bind:value={maxAscent} />
        </label>
      </div>

      <p class="eyebrow date-strip-title">Viable day</p>
      <div class="date-strip">
        <button
          type="button"
          class="day-chip"
          class:active={selectedDay == null}
          onclick={() => (selectedDay = null)}>Any</button
        >
        {#each dayKeys as key (key)}
          <button
            type="button"
            class="day-chip"
            class:active={selectedDay === key}
            onclick={() => (selectedDay = selectedDay === key ? null : key)}
            >{dayLabel(key)}</button
          >
        {/each}
      </div>

      <button type="button" class="reset" onclick={resetFilters}>Reset</button>
    </aside>

    <section class="map-region">
      <HikesMap walks={filtered} />
    </section>
  </div>

  <section class="list-view" aria-label="Walk list">
    <p class="eyebrow">{filtered.length} walks</p>
    <ul class="walk-list">
      {#each filtered as walk (walk.uuid)}
        <li class="walk-row card-hard">
          <a class="walk-row-name" href={walk.url} target="_blank" rel="noopener"
            >{walk.name} &nearr;</a
          >
          <p class="walk-row-stats">
            {walk.distance_km} km &middot; {walk.ascent_m} m &middot;
            {walk.duration_h} h
          </p>
        </li>
      {/each}
    </ul>
  </section>
</div>

<style>
  .hikes-page {
    position: relative;
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--cream);
    color: var(--ink);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  /* Header bar reads as a sibling of the ships breadcrumb chip: mono, uppercase,
     hard border. */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px 16px;
    padding: 12px 16px;
    background: var(--paper);
    border-bottom: 2px solid var(--ink);
  }

  .crumb {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .crumb-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-decoration-skip-ink: none;
    text-underline-offset: 2px;
    padding: 0 2px;
    transition: background 140ms ease;
  }

  .crumb-home:hover,
  .crumb-home:focus-visible {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
    text-decoration-color: var(--ink);
  }

  .crumb-arrow {
    font-size: 0.85em;
    margin-left: 1px;
  }

  .crumb-sep {
    color: var(--ink-3);
  }

  .topbar-stats {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  /* Map + sidebar fill the viewport under the header; the list view scrolls in
     below. */
  .layout {
    display: flex;
    height: calc(100dvh - 49px);
    min-height: 420px;
  }

  .sidebar {
    flex: none;
    width: 280px;
    padding: 16px;
    background: var(--paper);
    border-right: 2px solid var(--ink);
    overflow-y: auto;
  }

  .filter-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin: 10px 0 4px;
  }

  .filter-wide {
    grid-column: 1 / -1;
  }

  .sidebar label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .sidebar input {
    font-family: var(--mono);
    font-size: 13px;
    padding: 7px 8px;
    background: var(--cream);
    border: 2px solid var(--ink);
    color: var(--ink);
    width: 100%;
  }

  .date-strip-title {
    margin-top: 16px;
  }

  .date-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 8px 0 16px;
  }

  .day-chip {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 6px 9px;
    background: var(--paper);
    border: 2px solid var(--ink);
    color: var(--ink);
    cursor: pointer;
    transition:
      transform 110ms ease,
      box-shadow 110ms ease;
  }

  .day-chip:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }

  .day-chip.active {
    background: var(--ink);
    color: var(--paper);
  }

  .reset {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 10px 16px;
    background: var(--blue);
    border: 2px solid var(--ink);
    color: var(--ink);
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }

  .reset:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }

  /* HikesMap's .map-wrap is absolutely positioned, so this is its containing
     block. */
  .map-region {
    position: relative;
    flex: 1 1 auto;
    overflow: hidden;
  }

  .list-view {
    padding: 24px 16px 40px;
  }

  .walk-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px;
    margin-top: 12px;
  }

  .walk-row {
    padding: 14px 16px;
  }

  .walk-row-name {
    display: inline-block;
    font-family: var(--serif);
    font-size: 19px;
    line-height: 1.1;
    color: var(--ink);
  }

  .walk-row-stats {
    margin-top: 6px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    color: var(--ink-3);
  }

  @media (max-width: 760px) {
    /* Stack the sidebar above the map on narrow screens so neither is cramped. */
    .layout {
      flex-direction: column;
      height: auto;
    }

    .sidebar {
      width: auto;
      border-right: none;
      border-bottom: 2px solid var(--ink);
    }

    .map-region {
      height: 70vh;
      min-height: 360px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .day-chip,
    .reset {
      transition: none;
    }
  }
</style>
