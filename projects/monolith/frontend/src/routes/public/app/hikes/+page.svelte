<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import HikesMap from "$lib/public/components/hikes/HikesMap.svelte";
  import {
    filterWalksByCharacteristics,
    filterWalksByLocation,
    groupWindowsByDay,
    upcomingUkDays,
    viableInNextDays,
  } from "$lib/public/hikes/filters.js";

  let { data } = $props();

  let walks = $derived(data.snapshot?.walks ?? []);

  // A handful of Scottish hubs to anchor the "near" radius filter, so the
  // feature is useful without geolocation permission. Coordinates are the
  // town centres; the radius is generous enough that exact centring does not
  // matter. "__me__" is the sentinel for the device's own location.
  const HIKE_LOCATIONS = [
    { key: "edinburgh", label: "Edinburgh", lat: 55.9533, lon: -3.1883 },
    { key: "glasgow", label: "Glasgow", lat: 55.8642, lon: -4.2518 },
    { key: "stirling", label: "Stirling", lat: 56.1165, lon: -3.9369 },
    { key: "fort-william", label: "Fort William", lat: 56.8198, lon: -5.1052 },
    { key: "aviemore", label: "Aviemore", lat: 57.1958, lon: -3.8259 },
    { key: "inverness", label: "Inverness", lat: 57.4778, lon: -4.2247 },
    { key: "oban", label: "Oban", lat: 56.4153, lon: -5.4717 },
    { key: "pitlochry", label: "Pitlochry", lat: 56.7039, lon: -3.73 },
    { key: "aberdeen", label: "Aberdeen", lat: 57.1497, lon: -2.0943 },
  ];
  const GEO_SENTINEL = "__me__";

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

  // "Near" filter: a preset hub key, GEO_SENTINEL for the device location, or
  // "" for off. userCoords holds the resolved device position (null until the
  // browser grants permission); geoError surfaces a denial or unsupported API.
  let nearKey = $state("");
  let radiusKm = $state("50");
  let userCoords = $state(null);
  let geoError = $state("");

  function onNearChange() {
    geoError = "";
    if (nearKey === GEO_SENTINEL) {
      if (!navigator.geolocation) {
        geoError = "Geolocation not available";
        nearKey = "";
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          userCoords = {
            lat: pos.coords.latitude,
            lon: pos.coords.longitude,
          };
        },
        () => {
          geoError = "Location permission denied";
          nearKey = "";
          userCoords = null;
        },
      );
    } else {
      userCoords = null;
    }
  }

  // The active centre for the radius filter: device coords, a preset, or none.
  let nearCenter = $derived.by(() => {
    if (nearKey === GEO_SENTINEL) return userCoords; // null until permission resolves
    if (!nearKey) return null;
    return HIKE_LOCATIONS.find((l) => l.key === nearKey) ?? null;
  });

  // A coarse "current time" signal. The page can sit open for hours (the data
  // refetches every 30 min), so the day strip and "viable today" count must be
  // recomputed from a value that advances, otherwise they go stale across UK
  // midnight: the first "today" chip would point at a now-past day with zero
  // walks. We tick this a few times an hour (see onMount), which is plenty to
  // roll the calendar day over shortly after midnight.
  let nowMs = $state(Date.now());

  function numOr(value, fallback) {
    const n = parseFloat(value);
    return Number.isNaN(n) ? fallback : n;
  }

  let dayKeys = $derived(upcomingUkDays(DATE_HORIZON, new Date(nowMs)));

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

  // Then the radius filter (if a centre is set), which also annotates each walk
  // with distance_from_user and sorts nearest-first. Then the selected day (a
  // walk qualifies if it has any window on that UK day); .filter preserves the
  // nearest-first order so the list and map stay sorted.
  let filtered = $derived.by(() => {
    let result = byCharacteristics;
    if (nearCenter) {
      result = filterWalksByLocation(
        result,
        nearCenter.lat,
        nearCenter.lon,
        numOr(radiusKm, Infinity),
      );
    }
    if (selectedDay != null) {
      result = result.filter(
        (w) => (groupWindowsByDay(w)[selectedDay]?.length ?? 0) > 0,
      );
    }
    return result;
  });

  let locationActive = $derived(nearCenter != null);

  let viableTodayCount = $derived(
    walks.filter((w) => viableInNextDays(w, 1, new Date(nowMs))).length,
  );

  function resetFilters() {
    minDuration = "";
    maxDuration = "";
    minDistance = "";
    maxDistance = "";
    maxAscent = "";
    selectedDay = null;
    nearKey = "";
    radiusKm = "50";
    userCoords = null;
    geoError = "";
  }

  onMount(() => {
    // Live updates: re-run the SSR load on a 30 min timer (windows only change
    // 6-hourly, so this is plenty). No client-side call to /api/hikes/* happens.
    const refresh = setInterval(() => {
      nowMs = Date.now();
      invalidateAll();
    }, 30 * 60_000);
    // Roll the calendar day over shortly after midnight without waiting for the
    // next 30 min data refetch: a few-minute tick keeps the day strip and the
    // "viable today" count fresh on a long-open page. Deliberately low frequency
    // (not a per-second clock): nothing here needs sub-minute resolution.
    const dayTick = setInterval(() => (nowMs = Date.now()), 5 * 60_000);
    return () => {
      clearInterval(refresh);
      clearInterval(dayTick);
    };
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

      <p class="eyebrow near-title">Near</p>
      <div class="near-grid">
        <label class="near-where"
          >Location
          <select bind:value={nearKey} onchange={onNearChange}>
            <option value="">Anywhere</option>
            <option value={GEO_SENTINEL}>My location</option>
            {#each HIKE_LOCATIONS as loc (loc.key)}
              <option value={loc.key}>{loc.label}</option>
            {/each}
          </select>
        </label>
        <label class="near-radius"
          >Radius (km)
          <input
            type="number"
            min="1"
            step="10"
            bind:value={radiusKm}
            disabled={!nearKey}
          />
        </label>
      </div>
      {#if geoError}
        <p class="near-error" role="alert">{geoError}</p>
      {/if}

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
      <HikesMap walks={filtered} {selectedDay} />
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
            {#if locationActive && walk.distance_from_user != null}
              &middot; {Math.round(walk.distance_from_user)} km away
            {/if}
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

  .near-title {
    margin-top: 16px;
  }

  .near-grid {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
    margin: 10px 0 4px;
  }

  .near-radius input {
    width: 88px;
  }

  /* The native select inherits the mono/hard-border input look. */
  .sidebar select {
    font-family: var(--mono);
    font-size: 13px;
    padding: 7px 8px;
    background: var(--cream);
    border: 2px solid var(--ink);
    color: var(--ink);
    width: 100%;
  }

  .sidebar input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .near-error {
    margin: 4px 0 0;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.04em;
    color: var(--blue);
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
