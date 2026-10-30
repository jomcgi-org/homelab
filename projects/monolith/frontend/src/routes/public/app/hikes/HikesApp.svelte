<script>
  import { onMount } from "svelte";
  import { goto, invalidateAll } from "$app/navigation";
  import { page } from "$app/stores";
  import HikesMap from "$lib/public/components/hikes/HikesMap.svelte";
  import Button from "@homelab/design-system/components/Button.svelte";
  import {
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
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
  // Flat option list for the "Near" select: the two specials first, then the
  // density-ordered region presets.
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

{#snippet trail()}
  <Trail crumbs={[{ label: "hikes" }]} label="Breadcrumb" />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet hikes-page">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <div class="controls">
          <div class="edition control-head">
            <div class="ed-head">
              <p class="stats">
                {walks.length} walks &middot; {viableTodayCount} viable today &middot;
                {filtered.length} shown
              </p>
            </div>
          </div>

          <!-- Where: a region preset or the device location, in one select. -->
          <div class="edition near">
            <label class="near-field">
              <span class="field-label">Near</span>
              <select bind:value={nearKey} onchange={onNearChange}>
                {#each NEAR_OPTIONS as option (option.value)}
                  <option value={option.value}>{option.label}</option>
                {/each}
              </select>
            </label>
            {#if geoError}
              <p class="note" role="alert">{geoError}</p>
            {/if}
          </div>

          <!-- When: one chip per upcoming UK day with forecast coverage. -->
          <div
            class="edition days"
            role="group"
            aria-label="Filter by viable day"
          >
            <button
              type="button"
              class="day-chip day-chip-all"
              class:is-off={selectedDay != null}
              aria-pressed={selectedDay == null}
              onclick={() => (selectedDay = null)}>Any day</button
            >
            {#each dayKeys as key (key)}
              <button
                type="button"
                class="day-chip"
                class:is-off={selectedDay !== key}
                aria-pressed={selectedDay === key}
                onclick={() => (selectedDay = selectedDay === key ? null : key)}
                >{dayLabel(key)}</button
              >
            {/each}
          </div>

          <!-- How far: the numeric limits collapse by default so the map keeps
               the screen; the toggle shows a mark while a hidden one is set. -->
          <div class="edition limits">
            <button
              type="button"
              class="filter-toggle"
              aria-expanded={filtersOpen}
              onclick={() => (filtersOpen = !filtersOpen)}
            >
              <span class="filter-label">Limits</span>
              <span class="filter-current"
                >{filtersActive ? "set" : "none"}</span
              >
              <span
                class="filter-caret"
                class:open={filtersOpen}
                aria-hidden="true">&#9662;</span
              >
            </button>
            {#if filtersOpen}
              <div class="num-grid">
                <label class="field"
                  >Min duration (h)
                  <input
                    type="number"
                    min="0"
                    step="0.5"
                    bind:value={minDuration}
                  />
                </label>
                <label class="field"
                  >Max duration (h)
                  <input
                    type="number"
                    min="0"
                    step="0.5"
                    bind:value={maxDuration}
                  />
                </label>
                <label class="field"
                  >Min distance (km)
                  <input
                    type="number"
                    min="0"
                    step="1"
                    bind:value={minDistance}
                  />
                </label>
                <label class="field"
                  >Max distance (km)
                  <input
                    type="number"
                    min="0"
                    step="1"
                    bind:value={maxDistance}
                  />
                </label>
                <label class="field field-wide"
                  >Max ascent (m)
                  <input
                    type="number"
                    min="0"
                    step="50"
                    bind:value={maxAscent}
                  />
                </label>
              </div>
              <div class="reset-row">
                <Button variant="secondary" class="reset" onclick={resetFilters}
                  >Reset</Button
                >
              </div>
            {/if}
          </div>

          {#if walks.length > 0 && filtered.length === 0}
            <div class="edition empty-state" role="status">
              No walks match these filters. Reset the limits to see them all.
            </div>
          {/if}
        </div>
        <SiteIndex here="hikes" />
      </aside>
      <div class="content edition map-edition">
        <HikesMap
          walks={filtered}
          {selectedDay}
          {maxima}
          initialUuid={initial.selectedWalk}
          onSelectWalk={handleSelectWalk}
        />
      </div>
    </div>
  </div>
</main>

<style>
  .hikes-page {
    padding-bottom: 2rem;
  }

  /* The map wants the measure; the spine stays a control column. */
  .hikes-page .journal {
    grid-template-columns: minmax(13em, 16em) minmax(0, 1fr);
    gap: 1.5rem;
  }

  .controls {
    display: grid;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .stats {
    margin: 0;
    font: 0.75rem/1.6 var(--font-code);
    color: var(--ink-2);
  }

  .near-field {
    display: grid;
    gap: 0.35rem;
    padding: 0.6rem 0.75rem;
  }

  .field-label,
  .field {
    font: 0.68rem var(--font-code);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  /* Native controls, drawn as the sheet's: one hairline, the band as ground,
     the page's own type. The select keeps the platform's picker. */
  .near-field select,
  .field input {
    width: 100%;
    min-height: 44px;
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink);
    font: 0.85rem var(--font-ui);
    text-transform: none;
    letter-spacing: 0;
  }

  .near-field select {
    cursor: pointer;
  }

  .note {
    margin: 0;
    padding: 0 0.75rem 0.6rem;
    font: 0.72rem/1.5 var(--font-code);
    color: var(--ink-2);
  }

  /* Day choices form one divided value; the chosen day uses the accent and
     an underline, the same marker the stars night picker uses. */
  .days {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .day-chip {
    min-width: 0;
    min-height: 44px;
    padding: 0.5rem 0.2rem;
    background: var(--band);
    color: var(--accent-ink);
    border: 0;
    border-bottom: 1px solid var(--line);
    font: 0.75rem var(--font-code);
    text-align: center;
    text-decoration: underline;
    text-underline-offset: 0.25em;
    cursor: pointer;
  }

  .day-chip.is-off {
    background: var(--sheet);
    color: var(--ink-2);
    text-decoration: none;
  }

  .day-chip:hover,
  .filter-toggle:hover {
    background: var(--band);
    color: var(--ink);
  }

  .day-chip-all {
    grid-column: 1 / -1;
  }

  .filter-toggle {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    min-height: 44px;
    padding: 0.65rem 0.8rem;
    background: var(--sheet);
    border: 0;
    cursor: pointer;
    font: 0.75rem var(--font-code);
    color: var(--ink);
  }

  .filter-label {
    color: var(--ink-2);
  }

  .filter-current {
    margin-left: auto;
  }

  .filter-caret.open {
    transform: rotate(180deg);
  }

  .num-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.6rem;
    padding: 0.6rem 0.75rem;
    border-top: 1px solid var(--stroke);
  }

  .field {
    display: grid;
    gap: 0.3rem;
  }

  .field-wide {
    grid-column: 1 / -1;
  }

  .reset-row {
    padding: 0 0.75rem 0.75rem;
  }

  .reset-row :global(.reset) {
    font: 0.75rem var(--font-code);
  }

  .empty-state {
    padding: 0.8rem;
    font: 0.75rem/1.6 var(--font-code);
    color: var(--ink-2);
  }

  .map-edition {
    align-self: start;
    overflow: visible;
  }

  @media (max-width: 760px) {
    .hikes-page .journal {
      display: block;
    }

    .controls {
      margin-bottom: 1rem;
    }

    /* Keep the day strip to one row of chips on a phone; it scrolls if it
       truly overflows. */
    .days {
      display: flex;
      overflow-x: auto;
      scrollbar-width: none;
    }

    .day-chip {
      flex: 1 0 auto;
      padding-inline: 0.8rem;
    }
  }
</style>
