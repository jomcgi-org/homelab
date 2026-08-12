<script>
  import { onMount } from "svelte";
  import { goto, invalidateAll } from "$app/navigation";
  import { page } from "$app/stores";
  import HikesMap from "$lib/public/components/hikes/HikesMap.svelte";
  import BrutalistSelect from "$lib/public/components/BrutalistSelect.svelte";
  import {
    filterWalksByCharacteristics,
    filterWalksByLocation,
    upcomingUkDays,
  } from "$lib/public/hikes/filters.js";
  import {
    readHikeParams,
    writeHikeParams,
  } from "$lib/public/hikes/urlParams.js";

  let { data } = $props();

  let walks = $derived(data.snapshot?.walks ?? []);

  // Preset anchors for the "near" filter, the densest WalkHighlands regions by
  // walk count in the seed corpus (Fort William 128, Cairngorms 108, Argyll
  // 104, Perthshire 100, Galloway 82, Skye 76, Sutherland 68, Ullapool 66,
  // Loch Ness 65, Loch Lomond 64, Glasgow 64, Torridon/Wester Ross 59), not
  // population centres: a hike planner wants to jump to where the walks are,
  // and "My location" already covers "near where I live". Each coordinate is
  // the centroid of that region's walks. Ordered densest first. "__me__" is the
  // device-location sentinel.
  const HIKE_LOCATIONS = [
    { key: "fort-william", label: "Fort William", lat: 56.8326, lon: -5.251 },
    { key: "cairngorms", label: "Cairngorms", lat: 57.1232, lon: -3.631 },
    { key: "argyll", label: "Argyll & Oban", lat: 56.1519, lon: -5.2638 },
    { key: "perthshire", label: "Perthshire", lat: 56.5572, lon: -3.8982 },
    { key: "galloway", label: "Galloway", lat: 55.0211, lon: -4.08 },
    { key: "skye", label: "Skye", lat: 57.3649, lon: -6.2075 },
    { key: "sutherland", label: "Sutherland", lat: 58.3045, lon: -4.1337 },
    { key: "ullapool", label: "Ullapool & Assynt", lat: 57.9288, lon: -4.9246 },
    { key: "loch-ness", label: "Loch Ness", lat: 57.4078, lon: -4.5199 },
    { key: "loch-lomond", label: "Loch Lomond", lat: 56.2231, lon: -4.5265 },
    { key: "glasgow", label: "Glasgow", lat: 55.6912, lon: -4.3392 },
    { key: "torridon", label: "Wester Ross", lat: 57.5841, lon: -5.5325 },
  ];
  const GEO_SENTINEL = "__me__";
  // Flat option list for the brutalist "Near" dropdown: the two specials first,
  // then the density-ordered region presets.
  const NEAR_OPTIONS = [
    { value: "", label: "Anywhere" },
    { value: GEO_SENTINEL, label: "My location" },
    ...HIKE_LOCATIONS.map((l) => ({ value: l.key, label: l.label })),
  ];
  // Fixed radius for a region preset. The presets already pick a coarse area, so
  // a single generous radius beats a fiddly per-use input (regions span tens of
  // km; this captures the cluster without spilling into the next one).
  const REGION_RADIUS_KM = 75;

  // Valid "near" values for URL validation: the region preset keys plus the
  // device-location sentinel. A `near` param outside this set is ignored.
  const VALID_NEAR_KEYS = new Set([
    GEO_SENTINEL,
    ...HIKE_LOCATIONS.map((l) => l.key),
  ]);

  // Filter/view state is initialized from the URL on load (so a shared link
  // restores it) and mirrored back as it changes (see the $effect below). The
  // numeric fields stay strings to match the <input bind:value>; "" means "no
  // constraint" (the filters module reads undefined/NaN as unbounded).
  const initial = readHikeParams($page.url.searchParams, VALID_NEAR_KEYS);

  // The five numeric filters.
  let minDuration = $state(initial.minDuration);
  let maxDuration = $state(initial.maxDuration);
  let minDistance = $state(initial.minDistance);
  let maxDistance = $state(initial.maxDistance);
  let maxAscent = $state(initial.maxAscent);

  // Date strip: one chip per upcoming UK-local day. null means "any day".
  const DATE_HORIZON = 7;
  let selectedDay = $state(initial.selectedDay);

  // The open route card's walk uuid, mirrored to ?walk= so a specific hike is
  // deep-linkable. HikesMap owns the live card; it calls onSelectWalk on
  // open/close to keep this in sync, and opens initial.selectedWalk on load.
  let selectedWalkUuid = $state(initial.selectedWalk);
  function handleSelectWalk(uuid) {
    selectedWalkUuid = uuid;
  }

  // "Near" filter: a preset hub key, GEO_SENTINEL for the device location, or
  // "" for off. userCoords resolves async once the browser grants permission.
  let nearKey = $state(initial.nearKey);
  let userCoords = $state(null);
  let geoError = $state("");

  // The extra filters (region + numeric) collapse by default so the map keeps
  // the screen; only the day chips stay permanently visible.
  let filtersOpen = $state(false);

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
          userCoords = { lat: pos.coords.latitude, lon: pos.coords.longitude };
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

  let nearCenter = $derived.by(() => {
    if (nearKey === GEO_SENTINEL) return userCoords; // null until permission resolves
    if (!nearKey) return null;
    return HIKE_LOCATIONS.find((l) => l.key === nearKey) ?? null;
  });

  // A coarse "current time" signal so the day strip and "viable today" count
  // roll over UK midnight on a long-open page (see the tick in onMount).
  let nowMs = $state(Date.now());

  function numOr(value, fallback) {
    const n = parseFloat(value);
    return Number.isNaN(n) ? fallback : n;
  }

  // The days any walk is actually doable on (union of viable_days across the
  // corpus). met.no's compact forecast only carries hourly data for ~3 days, so
  // the corpus only covers ~today..+2; without this the strip would render dead
  // chips for days 4-7 that match no walk ("picking a day shows nothing").
  let availableDays = $derived(
    new Set(walks.flatMap((w) => w.viable_days ?? [])),
  );

  // The upcoming UK days that have forecast coverage: the rolling N-day strip
  // intersected with days the data actually has. Every chip shown yields walks.
  let dayKeys = $derived(
    upcomingUkDays(DATE_HORIZON, new Date(nowMs)).filter((k) =>
      availableDays.has(k),
    ),
  );

  function dayLabel(key) {
    // Noon UTC keeps the label on the intended UK calendar day across BST/GMT.
    return new Date(`${key}T12:00:00Z`).toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      timeZone: "Europe/London",
    });
  }

  // Difficulty normalization ceilings: the 95th percentile of each metric over
  // the WHOLE corpus (not the filtered set), so the colour a walk gets is its
  // intrinsic effort and does not shift as you filter. The p95 (rather than max)
  // keeps a handful of epic routes from compressing everything else to "easy".
  function pct(values, p) {
    const s = values
      .filter((v) => v != null && !Number.isNaN(v))
      .sort((a, b) => a - b);
    if (!s.length) return 1;
    return s[Math.min(s.length - 1, Math.floor(p * s.length))] || 1;
  }
  let maxima = $derived({
    duration: pct(
      walks.map((w) => w.duration_h),
      0.95,
    ),
    ascent: pct(
      walks.map((w) => w.ascent_m),
      0.95,
    ),
    distance: pct(
      walks.map((w) => w.distance_km),
      0.95,
    ),
  });

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

  // Then the region radius (if a centre is set; annotates distance_from_user and
  // sorts nearest-first), then the selected day (.filter preserves that order).
  let filtered = $derived.by(() => {
    let result = byCharacteristics;
    if (nearCenter) {
      result = filterWalksByLocation(
        result,
        nearCenter.lat,
        nearCenter.lon,
        REGION_RADIUS_KM,
      );
    }
    if (selectedDay != null) {
      // viable_days is the server-computed set of UK days the walk is DOABLE on:
      // days whose good hours hold a slot long enough for the walk's duration
      // (not merely one good hour). Membership is a cheap O(1) check, no hourly
      // windows to re-parse.
      result = result.filter((w) => w.viable_days?.includes(selectedDay));
    } else {
      // "Any day": still drop walks doable on NO forecast day, so the map means
      // "doable somewhere in the 7-day window" rather than plotting walks whose
      // whole forecast is too short/washed-out to ever complete.
      result = result.filter((w) => w.viable_days?.length);
    }
    return result;
  });

  // Today's UK-local day key (first chip of the rolling strip).
  let todayKey = $derived(upcomingUkDays(1, new Date(nowMs))[0]);
  let viableTodayCount = $derived(
    walks.filter((w) => w.viable_days?.includes(todayKey)).length,
  );

  // A hidden (collapsed) numeric filter is active, so the toggle can show a dot.
  // Near lives in the always-visible head, so it does not count here.
  let filtersActive = $derived(
    !!(minDuration || maxDuration || minDistance || maxDistance || maxAscent),
  );

  function resetFilters() {
    minDuration = "";
    maxDuration = "";
    minDistance = "";
    maxDistance = "";
    maxAscent = "";
    selectedDay = null;
    nearKey = "";
    userCoords = null;
    geoError = "";
  }

  // Mirror the filter state back to the URL so it is shareable. replaceState so
  // typing in a number field or stepping the day strip does not spam history.
  // Guarded: only goto when the serialized params actually differ from the
  // current URL, so this "URL write" never re-triggers the init read in a loop.
  $effect(() => {
    const url = new URL($page.url);
    writeHikeParams(url.searchParams, {
      selectedDay,
      selectedWalk: selectedWalkUuid,
      nearKey,
      minDuration,
      maxDuration,
      minDistance,
      maxDistance,
      maxAscent,
    });
    if (url.searchParams.toString() !== $page.url.searchParams.toString()) {
      goto(url, { keepFocus: true, noScroll: true, replaceState: true });
    }
  });

  onMount(() => {
    // If a shared link restored the "my location" sentinel, resolve the device
    // position so the region radius applies (the dropdown shows it as selected,
    // but nearCenter is null until coords arrive).
    if (nearKey === GEO_SENTINEL) onNearChange();

    // Live updates: re-run the SSR load every 30 min (windows change 6-hourly).
    const refresh = setInterval(() => {
      nowMs = Date.now();
      invalidateAll();
    }, 30 * 60_000);
    // Roll the UK calendar day shortly after midnight without waiting for the
    // data refetch; low frequency, nothing here needs sub-minute resolution.
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
    content="A map of Scottish hill walks coloured by effort and filtered by viable weather windows from the met.no forecast."
  />
</svelte:head>

<div class="hikes-page">
  <h1 class="sr-only">Hike planner, Scotland walks by weather window</h1>

  <HikesMap
    walks={filtered}
    {selectedDay}
    {maxima}
    initialUuid={initial.selectedWalk}
    onSelectWalk={handleSelectWalk}
  />

  <!-- Floating controls: day chips always visible, the rest expands on demand. -->
  <div class="controls">
    <div class="panel control-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true"
              >&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">hikes</span>
        </nav>
        <p class="stats">
          {walks.length} walks &middot; {viableTodayCount} viable today &middot;
          {filtered.length} shown
        </p>
      </div>

      <div class="field near-field">
        <span class="field-label" id="near-label">Near</span>
        <BrutalistSelect
          id="near"
          label="Near"
          options={NEAR_OPTIONS}
          bind:value={nearKey}
          onchange={onNearChange}
        />
      </div>
      {#if geoError}
        <p class="geo-error" role="alert">{geoError}</p>
      {/if}

      <div class="day-strip" role="group" aria-label="Filter by viable day">
        <button
          type="button"
          class="day-chip"
          class:active={selectedDay == null}
          onclick={() => (selectedDay = null)}>Any day</button
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

      <button
        type="button"
        class="more-toggle"
        class:on={filtersOpen}
        aria-expanded={filtersOpen}
        onclick={() => (filtersOpen = !filtersOpen)}
      >
        Filters{#if filtersActive && !filtersOpen}<span
            class="dot"
            aria-hidden="true"
          ></span>{/if}
        <span class="chev" aria-hidden="true">{filtersOpen ? "▴" : "▾"}</span>
      </button>
      {#if filtered.length === 0}
        <p class="geo-error">
          No walks match these filters. Reset to see them all.
        </p>
      {/if}
    </div>

    {#if filtersOpen}
      <div class="panel control-more">
        <div class="num-grid">
          <label class="field"
            >Min duration (h)
            <input type="number" min="0" step="0.5" bind:value={minDuration} />
          </label>
          <label class="field"
            >Max duration (h)
            <input type="number" min="0" step="0.5" bind:value={maxDuration} />
          </label>
          <label class="field"
            >Min distance (km)
            <input type="number" min="0" step="1" bind:value={minDistance} />
          </label>
          <label class="field"
            >Max distance (km)
            <input type="number" min="0" step="1" bind:value={maxDistance} />
          </label>
          <label class="field field-wide"
            >Max ascent (m)
            <input type="number" min="0" step="50" bind:value={maxAscent} />
          </label>
        </div>

        <button type="button" class="reset" onclick={resetFilters}>Reset</button
        >
      </div>
    {/if}
  </div>
</div>

<style>
  /* Full-bleed, map-first (same shell as /app/ships): the map owns the viewport
     and every control floats over it. HikesMap's .map-wrap is absolutely
     positioned, so this is its containing block. */
  .hikes-page {
    position: relative;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
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

  /* Floating control stack, top-left, clear of the map's own chrome. */
  .controls {
    position: absolute;
    top: 16px;
    left: 16px;
    z-index: 5;
    display: flex;
    flex-direction: column;
    gap: 10px;
    width: min(420px, calc(100% - 32px));
  }

  /* Flat sharp-bordered overlay, matching the ships map overlays: paper bg,
     2px ink border, no border-radius, no drop shadow. */
  .panel {
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 12px;
  }

  .control-head {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .crumb-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 14px;
    flex-wrap: wrap;
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

  .stats {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  /* Day chips wrap, and scroll horizontally only if they truly overflow on a
     very narrow screen. */
  .day-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
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
    white-space: nowrap;
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

  .more-toggle {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 7px 11px;
    background: var(--paper);
    border: 2px solid var(--ink);
    color: var(--ink);
    cursor: pointer;
    transition:
      transform 110ms ease,
      box-shadow 110ms ease;
  }

  .more-toggle:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }

  .more-toggle.on {
    background: var(--ink);
    color: var(--paper);
  }

  .more-toggle .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--blue);
  }

  .more-toggle .chev {
    font-size: 10px;
  }

  .control-more {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .num-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }

  .field-wide {
    grid-column: 1 / -1;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .field input {
    font-family: var(--mono);
    font-size: 13px;
    padding: 7px 8px;
    background: var(--cream);
    border: 2px solid var(--ink);
    color: var(--ink);
    width: 100%;
  }

  .geo-error {
    margin: 0;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.04em;
    color: var(--blue);
  }

  .reset {
    align-self: flex-start;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 9px 16px;
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

  @media (max-width: 640px) {
    .controls {
      top: 12px;
      left: 12px;
      width: calc(100% - 24px);
    }

    /* Keep the day strip on one swipeable line so the panel stays short. */
    .day-strip {
      flex-wrap: nowrap;
      overflow-x: auto;
      scrollbar-width: none;
    }

    .day-strip::-webkit-scrollbar {
      display: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .day-chip,
    .more-toggle,
    .reset {
      transition: none;
    }
  }
</style>
