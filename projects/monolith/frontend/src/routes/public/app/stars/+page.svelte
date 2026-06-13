<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import StarsMap from "$lib/public/components/stars/StarsMap.svelte";
  import { monthLabel, monthShort } from "$lib/public/stars/heat.js";

  let { data } = $props();

  let sites = $derived(data.snapshot?.sites ?? []);
  let count = $derived(data.snapshot?.count ?? 0);

  // Mode: LIVE = the upcoming-forecast layer (per-night quality); HISTORICAL =
  // the month-bucketed accumulated quality layer (ADR 008). The toggle swaps the
  // map's data source, the time control (night picker vs month picker), and the
  // heat field StarsMap weights by.
  let mode = $state("live");

  // Heat layer: in LIVE it is an optional overlay (markers-first by default, the
  // shipped look), so it is off until toggled; in HISTORICAL it is the point of
  // the layer, so it is always on. StarsMap reads the resolved value.
  let showHeat = $state(false);
  let heatOn = $derived(mode === "historical" ? true : showHeat);

  // Night picker (LIVE): "I'm free Saturday, show me the map for Saturday night."
  // One chip per viewing night plus an "All" reset; picking a night filters the
  // map to that night and StarsMap recolours each marker by the score it reaches
  // then. `nights` is the sorted union of evening dates the API returns.
  let nights = $derived(data.snapshot?.nights ?? []);
  let selectedNight = $state("all"); // "all" or a night key
  let nightOpen = $state(false); // the night box is collapsed until tapped

  // Fall back to "all" if the chosen night has dropped off the forecast horizon
  // on an SSR refresh (it elapsed). Derived, so there is no effect to loop on.
  let effectiveNight = $derived(
    selectedNight !== "all" && nights.includes(selectedNight)
      ? selectedNight
      : "all",
  );

  // The nights the map scores against: every night for "all", else just the
  // chosen one. StarsMap takes the max score across this set per marker.
  let activeNights = $derived(
    effectiveNight === "all" ? new Set(nights) : new Set([effectiveNight]),
  );

  // Format a night key (the evening date) into a short "Sat 14" chip label.
  // Noon UTC keeps the weekday/day from rolling across the date line when
  // rendered in UK local time.
  function nightLabel(key) {
    const [y, m, d] = key.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d, 12));
    return dt.toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      timeZone: "Europe/London",
    });
  }

  function selectNight(key) {
    selectedNight = key;
    nightOpen = false; // collapse back to the summary once a night is picked
  }

  // Month picker (HISTORICAL): one chip per month-of-year, defaulting to the
  // current UTC month (the accumulator buckets by month-of-year, not year-month,
  // so the picker is a fixed Jan..Dec, ADR 008).
  const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1);
  let selectedMonth = $state(new Date().getUTCMonth() + 1);
  let monthOpen = $state(false);

  function selectMonth(m) {
    selectedMonth = m;
    monthOpen = false;
  }

  // Historical data, fetched through the SSR-only same-origin proxy
  // (/app/stars/history/<month>) so the browser never touches /api/stars/*.
  // Cached per month so re-selecting a month is instant.
  const historyCache = new Map();
  let historyData = $state(null); // last loaded {month, sites, count}
  let historyLoading = $state(false);
  let historyError = $state(false);

  async function loadMonth(m) {
    if (historyCache.has(m)) {
      historyData = historyCache.get(m);
      historyError = false;
      return;
    }
    historyLoading = true;
    historyError = false;
    try {
      const res = await fetch(`/app/stars/history/${m}`);
      if (!res.ok) throw new Error(`history ${res.status}`);
      const payload = await res.json();
      historyCache.set(m, payload);
      // Only apply if the user has not moved on while the fetch was in flight.
      if (mode === "historical" && selectedMonth === m) {
        historyData = payload;
      }
    } catch {
      if (mode === "historical" && selectedMonth === m) historyError = true;
    } finally {
      historyLoading = false;
    }
  }

  // Load (or re-use) the selected month whenever we are in historical mode or the
  // month changes. Reads mode + selectedMonth, so it re-runs on either; it does
  // not read the history state it writes, so there is no loop.
  $effect(() => {
    if (mode === "historical") loadMonth(selectedMonth);
  });

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    if (next === "live") showHeat = false; // back to markers-first live default
  }

  // The site set the map plots: live snapshot, or the loaded month's sites.
  let mapSites = $derived(mode === "live" ? sites : (historyData?.sites ?? []));

  // Whether the loaded history matches the currently selected month (guards the
  // header + empty state from showing stale counts during a month switch).
  let histReady = $derived(
    !!historyData && historyData.month === selectedMonth,
  );
  let histCount = $derived(histReady ? historyData.count : 0);

  // sites is already sorted by best_score descending, so the head is the best.
  let topScore = $derived(
    sites.length ? Math.round(sites[0].best_score ?? 0) : null,
  );

  // A coarse "current time" signal: it advances the "updated Xm ago" label and
  // lets StarsMap drop hours that elapse on a long-open page (see the tick in
  // onMount).
  let nowMs = $state(Date.now());

  // Relative age of the snapshot, mirroring the homepage's formatAgo (a local
  // helper, not a shared export). Parameterized on nowMs so it ticks.
  function formatAgo(iso, now) {
    const then = Date.parse(iso);
    if (!Number.isFinite(then)) return null;
    const minutes = Math.max(0, Math.round((now - then) / 60_000));
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours}h`;
    return `${Math.round(hours / 24)}d`;
  }

  let agoLabel = $derived(formatAgo(data.snapshot?.fetched_at, nowMs));

  onMount(() => {
    // Live updates: re-run the SSR load every 30 min (the refresh job runs
    // 3-hourly). Same pattern as /app/hikes.
    const refresh = setInterval(() => {
      nowMs = Date.now();
      invalidateAll();
    }, 30 * 60_000);
    // Lower-frequency tick so the age label advances and elapsed hours drop out
    // of the open card without waiting for the data refetch; nothing here needs
    // sub-minute resolution.
    const clockTick = setInterval(() => (nowMs = Date.now()), 5 * 60_000);
    return () => {
      clearInterval(refresh);
      clearInterval(clockTick);
    };
  });
</script>

<svelte:head>
  <title>Dark-sky stargazing map, Scotland viewing windows</title>
  <meta
    name="description"
    content="A map of curated Scottish dark-sky sites scored by upcoming viewing windows from the met.no forecast, with a historical realized-quality layer by month."
  />
</svelte:head>

<div class="stars-page">
  <h1 class="sr-only">Dark-sky stargazing map, Scotland viewing windows</h1>

  <StarsMap sites={mapSites} {activeNights} {nowMs} {mode} heatVisible={heatOn} />

  <!-- Floating header: breadcrumb + headline stats, top-left clear of the map
       chrome (mirrors the hikes control head). -->
  <div class="controls">
    <div class="panel control-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="https://jomcgi.dev/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">stars</span>
        </nav>
        {#if mode === "historical"}
          <p class="stats">
            {histCount}
            {histCount === 1 ? "site" : "sites"} with history in {monthLabel(
              selectedMonth,
            )}
          </p>
        {:else}
          <p class="stats">
            {count} dark-sky sites{#if topScore != null}
              &middot; best score {topScore}{/if}{#if agoLabel}
              &middot; updated {agoLabel} ago{/if}
          </p>
        {/if}
      </div>
    </div>

    <!-- Mode toggle: LIVE forecast vs HISTORICAL realized-quality (ADR 008). -->
    <div class="panel mode-toggle" role="group" aria-label="Map mode">
      <button
        type="button"
        class="seg"
        class:is-active={mode === "live"}
        aria-pressed={mode === "live"}
        onclick={() => setMode("live")}
      >
        Live
      </button>
      <button
        type="button"
        class="seg"
        class:is-active={mode === "historical"}
        aria-pressed={mode === "historical"}
        onclick={() => setMode("historical")}
      >
        Historical
      </button>
    </div>

    {#if mode === "live"}
      <!-- Heat overlay switch (live only): historical forces heat on. -->
      <button
        type="button"
        class="panel heat-switch"
        class:is-on={showHeat}
        aria-pressed={showHeat}
        onclick={() => (showHeat = !showHeat)}
      >
        <span class="heat-label">Heatmap</span>
        <span class="heat-state">{showHeat ? "On" : "Off"}</span>
      </button>

      {#if nights.length > 1}
        <div class="panel night-filter">
          <button
            type="button"
            class="filter-toggle"
            aria-expanded={nightOpen}
            onclick={() => (nightOpen = !nightOpen)}
          >
            <span class="filter-label">Night</span>
            <span class="filter-current"
              >{effectiveNight === "all"
                ? "All nights"
                : nightLabel(effectiveNight)}</span
            >
            <span class="filter-caret" class:open={nightOpen} aria-hidden="true"
              >&#9662;</span
            >
          </button>
          {#if nightOpen}
            <div class="night-chips">
              <button
                type="button"
                class="night-chip night-chip-all"
                class:is-off={effectiveNight !== "all"}
                aria-pressed={effectiveNight === "all"}
                onclick={() => selectNight("all")}
              >
                All nights
              </button>
              {#each nights as night (night)}
                <button
                  type="button"
                  class="night-chip"
                  class:is-off={effectiveNight !== night}
                  aria-pressed={effectiveNight === night}
                  onclick={() => selectNight(night)}
                >
                  {nightLabel(night)}
                </button>
              {/each}
            </div>
          {/if}
        </div>
      {/if}

      {#if count === 0}
        <div class="panel empty-state" role="status">
          No dark-sky windows in the next few nights. Check back after the next
          forecast refresh.
        </div>
      {/if}
    {:else}
      <!-- Month picker (historical): a collapsed summary that expands into a
           four-up grid of month chips, mirroring the night picker. -->
      <div class="panel month-filter">
        <button
          type="button"
          class="filter-toggle"
          aria-expanded={monthOpen}
          onclick={() => (monthOpen = !monthOpen)}
        >
          <span class="filter-label">Month</span>
          <span class="filter-current">{monthLabel(selectedMonth)}</span>
          <span class="filter-caret" class:open={monthOpen} aria-hidden="true"
            >&#9662;</span
          >
        </button>
        {#if monthOpen}
          <div class="month-chips">
            {#each MONTHS as m (m)}
              <button
                type="button"
                class="month-chip"
                class:is-off={selectedMonth !== m}
                aria-pressed={selectedMonth === m}
                onclick={() => selectMonth(m)}
              >
                {monthShort(m)}
              </button>
            {/each}
          </div>
        {/if}
      </div>

      {#if historyError}
        <div class="panel empty-state" role="status">
          Historical data is unavailable right now. Try another month or check
          back shortly.
        </div>
      {:else if historyLoading && !histReady}
        <div class="panel empty-state" role="status">
          Loading {monthLabel(selectedMonth)} history&hellip;
        </div>
      {:else if histReady && histCount === 0}
        <div class="panel empty-state" role="status">
          Historical data is still accumulating for {monthLabel(selectedMonth)}.
          Quality banks as forecast hours elapse, so this layer fills over the
          coming weeks.
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  /* Full-bleed, map-first (same shell as /app/ships + /app/hikes): the map owns
     the viewport and every control floats over it. StarsMap's .map-wrap is
     absolutely positioned, so this is its containing block. --paper is the light
     base so the load flash matches the light liberty basemap, not a dark flash. */
  .stars-page {
    position: relative;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    background: var(--paper);
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

  /* Flat sharp-bordered overlay, matching the ships + hikes map overlays:
     paper bg, 2px ink border, no border-radius. */
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

  .empty-state {
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.5;
    letter-spacing: 0.02em;
    color: var(--ink-2);
  }

  /* Mode toggle: two equal segments, the active one filled ink-on-paper, the
     other inverted, sharing one border (neobrutalist segmented control). */
  .mode-toggle {
    display: flex;
    padding: 0;
    overflow: hidden;
  }

  .seg {
    flex: 1;
    padding: 10px 12px;
    background: var(--paper);
    color: var(--ink);
    border: none;
    cursor: pointer;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    transition: background 110ms ease;
  }

  .seg + .seg {
    border-left: 2px solid var(--ink);
  }

  .seg.is-active {
    background: var(--ink);
    color: var(--paper);
  }

  .seg:not(.is-active):hover,
  .seg:not(.is-active):focus-visible {
    background: var(--cream);
  }

  /* Heat switch: a single full-width pill mirroring the filter toggle row; fills
     accent when on so it reads as engaged. */
  .heat-switch {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    font-family: var(--mono);
    text-align: left;
    transition: background 110ms ease;
  }

  .heat-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .heat-state {
    margin-left: auto;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
  }

  .heat-switch.is-on {
    background: var(--accent);
  }

  .heat-switch.is-on .heat-label,
  .heat-switch.is-on .heat-state {
    color: var(--ink);
  }

  /* Time filters (night + month): a collapsed summary row that expands on click
     into a grid of chips, so it stays out of the way until reached for. */
  .night-filter,
  .month-filter {
    padding: 0;
  }

  .filter-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 10px 12px;
    background: var(--paper);
    border: none;
    cursor: pointer;
    font-family: var(--mono);
    color: var(--ink);
  }

  .filter-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .filter-current {
    margin-left: auto;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
  }

  .filter-caret {
    font-size: 11px;
    color: var(--ink-2);
    transition: transform 140ms ease;
  }

  .filter-caret.open {
    transform: rotate(180deg);
  }

  /* Grid of chips, revealed below the summary when expanded; the top rule
     separates it from the toggle row. Nights are two-up, months four-up. */
  .night-chips {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    padding: 10px 12px 12px;
    border-top: 2px solid var(--ink);
  }

  .month-chips {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 6px;
    padding: 10px 12px 12px;
    border-top: 2px solid var(--ink);
  }

  .night-chip,
  .month-chip {
    padding: 6px 9px;
    background: var(--ink);
    color: var(--paper);
    border: 2px solid var(--ink);
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-align: center;
    cursor: pointer;
    transition:
      transform 110ms ease,
      box-shadow 110ms ease,
      opacity 110ms ease,
      background 110ms ease;
  }

  .night-chip:hover,
  .night-chip:focus-visible,
  .month-chip:hover,
  .month-chip:focus-visible {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }

  .night-chip:active,
  .month-chip:active {
    transform: translate(-1px, -1px);
    box-shadow: 1px 1px 0 var(--ink);
  }

  /* The picked chip is filled; the rest invert to paper + dim so the current
     selection reads at a glance while staying tappable. */
  .night-chip.is-off,
  .month-chip.is-off {
    background: var(--paper);
    color: var(--ink);
    opacity: 0.55;
  }

  /* The night reset spans the full width above the per-night grid. */
  .night-chip-all {
    grid-column: 1 / -1;
  }

  @media (max-width: 640px) {
    .controls {
      top: 12px;
      left: 12px;
      width: calc(100% - 24px);
    }
  }
</style>
