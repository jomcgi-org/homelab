<script>
  import { onMount } from "svelte";
  import { goto, invalidateAll } from "$app/navigation";
  import { page } from "$app/stores";
  import StarsMap from "$lib/public/components/stars/StarsMap.svelte";
  import {
    historyView,
    monthLabel,
    monthShort,
    starsNights,
  } from "$lib/public/stars/heat.js";
  import {
    readStarsParams,
    writeStarsParams,
  } from "$lib/public/stars/urlParams.js";

  // View state (mode + time selection) is initialized from the URL on load and
  // mirrored back as it changes (see the $effect below), so a shared link
  // restores it.
  let { data } = $props();
  const initialView = readStarsParams($page.url.searchParams);

  // A coarse "current time" signal: it advances the "updated Xm ago" label, the
  // night-chip set, and StarsMap dropping hours that elapse on a long-open page
  // (see the tick in onMount). Declared up top so the derivations below can read
  // it.
  let nowMs = $state(Date.now());

  let sites = $derived(data.snapshot?.sites ?? []);
  let count = $derived(data.snapshot?.count ?? 0);

  // Page-level darkness mode from the live snapshot (stars twilight fallback):
  //   "astronomical" - true dark (sun < -12) is available somewhere tonight;
  //   "twilight"     - no true dark anywhere, only the -10 deg twilight floor;
  //   "none"         - not even twilight (deep midsummer in the far north).
  // For ~7 weeks each midsummer Scotland has no astronomical darkness, so this
  // drives the disclaimer and tells StarsMap to colour by the twilight count.
  // Defaults to "astronomical" so an older/cached payload without the field
  // behaves exactly as before (no disclaimer, dark-hour colouring).
  let darkness = $derived(data.snapshot?.darkness ?? "astronomical");

  // Mode: LIVE = the upcoming-forecast layer (per-night quality); HISTORICAL =
  // the month-bucketed clear-dark-hours layer (stars v2). Both render as the
  // box-cell heatmap (see heatOn below). The toggle swaps the map's data source
  // and the time control (night picker vs month picker). Initialized from the
  // URL (?mode=) so a shared link opens on the right layer.
  let mode = $state(initialView.mode);

  // Both layers render as the box-cell heatmap. The site grid is a dense ~4km
  // mesh (thousands of sites), so plotting raw point markers piles them into
  // unreadable blobs; the filled cells read cleanly at every zoom (ADR 009,
  // mirroring /app/ships). StarsMap still opens the per-site card on a cell tap,
  // so the live forecast windows table is unaffected. Always on, both modes.
  const heatOn = true;

  // Night picker (LIVE): "I'm free Saturday, show me the map for Saturday night."
  // One chip per viewing night plus an "All" reset; picking a night filters the
  // map to that night and StarsMap recolours each marker by the score it reaches
  // then. `nights` is the sorted union of upcoming evening dates, derived from
  // the same windows the map colours by (and re-derived as nowMs advances, so a
  // fully-elapsed night drops its chip). Empty unless we are in live mode.
  let nights = $derived(
    mode === "live" ? starsNights(sites, darkness, nowMs) : [],
  );
  let selectedNight = $state(initialView.selectedNight); // "all" or a night key
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

  // Month picker (HISTORICAL): an "All year" option (0) plus one chip per
  // month-of-year. Defaults to All year so the page opens on the full seasonal
  // picture from the ERA5 climatology (ADR 009); month 0 makes the API sum every
  // month bucket per site. The accumulator buckets by month-of-year, not
  // year-month, so the picker is a fixed All / Jan..Dec (ADR 008).
  const ALL_YEAR = 0;
  const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1);
  let selectedMonth = $state(initialView.selectedMonth);
  let monthOpen = $state(false);

  // Display label for the current selection, and the phrase used in the header.
  let monthSummary = $derived(
    selectedMonth === ALL_YEAR ? "All year" : monthLabel(selectedMonth),
  );
  let historyScope = $derived(
    selectedMonth === ALL_YEAR
      ? "across the year"
      : `in ${monthLabel(selectedMonth)}`,
  );

  function selectMonth(m) {
    selectedMonth = m;
    monthOpen = false;
  }

  // Historical data: the WHOLE climatology (every site's 12-month clear-dark
  // hours) fetched once through the SSR-only same-origin proxy
  // (/app/stars/history) so the browser never touches /api/stars/*. The month
  // picker then filters this in memory (see mapSites below), so switching months
  // is instant and never hits the network. One payload, one cache entry, instead
  // of a per-month request that re-queried (and OOM-killed) the backend each time.
  let historyAllSites = $state(null); // [{id,name,lat,lon,clear:[12],dark:[12]}]
  let historyLoading = $state(false); // template-facing only
  let historyError = $state(false);
  // Plain (non-reactive) guards. Keeping them out of $state means the loadHistory
  // effect depends only on `mode`, never on these: a persistent fetch failure
  // does not re-trigger the effect into a retry loop (which would hammer the
  // backend during the very outage that caused the failure). A retry happens only
  // when the user toggles back into historical mode.
  let historyLoaded = false; // succeeded once
  let historyInFlight = false; // a fetch is running

  async function loadHistory() {
    if (historyLoaded || historyInFlight) return;
    historyInFlight = true;
    historyLoading = true;
    historyError = false;
    try {
      const res = await fetch(`/app/stars/history-map`);
      if (!res.ok) throw new Error(`history ${res.status}`);
      const payload = await res.json();
      historyAllSites = payload.sites ?? [];
      historyLoaded = true;
    } catch {
      historyError = true;
    } finally {
      historyInFlight = false;
      historyLoading = false;
    }
  }

  // Fetch the history payload the first time we enter historical mode. Reads
  // mode; does not read the history state it writes, so there is no loop. A
  // failed load leaves historyLoaded false, so toggling back into historical
  // retries.
  $effect(() => {
    if (mode === "historical") loadHistory();
  });

  function setMode(next) {
    if (next === mode) return;
    mode = next;
  }

  // Mirror the view state back to the URL so it is shareable. Uses effectiveNight
  // (not raw selectedNight) so a night that dropped off the forecast horizon is
  // not persisted. replaceState to keep mode/time flips out of browser history.
  // Guarded: only goto when the serialized params differ from the current URL,
  // so this "URL write" never re-triggers the init read in a loop.
  $effect(() => {
    const url = new URL($page.url);
    writeStarsParams(url.searchParams, {
      mode,
      selectedNight: effectiveNight,
      selectedMonth,
    });
    if (url.searchParams.toString() !== $page.url.searchParams.toString()) {
      goto(url, { keepFocus: true, noScroll: true, replaceState: true });
    }
  });

  // The site set the map plots: live snapshot, or the all-months history
  // projected onto the selected month (client-side, instant on a month switch).
  let mapSites = $derived(
    mode === "live" ? sites : historyView(historyAllSites, selectedMonth),
  );

  // Whether the one-time history payload has loaded, and how many sites have data
  // in the currently selected view (drives the header count + empty state).
  let histReady = $derived(!!historyAllSites);
  let histCount = $derived(mode === "historical" ? mapSites.length : 0);

  // sites is already sorted by clear_dark_hours descending, so the head is the
  // site with the most upcoming clear-dark hours.
  let topClearDark = $derived(
    sites.length ? Math.round(sites[0].clear_dark_hours ?? 0) : null,
  );

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
    // Warm the tiny historical visual payload after the live page has mounted.
    // Historical mode then swaps instantly for most visitors without putting
    // its request on the critical path.
    const warmHistory = () => loadHistory();
    const idle = window.requestIdleCallback
      ? window.requestIdleCallback(warmHistory, { timeout: 2000 })
      : window.setTimeout(warmHistory, 1000);
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
      if (window.cancelIdleCallback && typeof idle === "number") {
        window.cancelIdleCallback(idle);
      } else {
        window.clearTimeout(idle);
      }
      clearInterval(refresh);
      clearInterval(clockTick);
    };
  });
</script>

<svelte:head>
  <title>Dark-sky stargazing map, Scotland viewing windows</title>
  <meta
    name="description"
    content="A map of curated Scottish dark-sky sites scored by upcoming viewing windows from the met.no forecast, with a historical clear-dark-hours layer by month."
  />
</svelte:head>

<div class="stars-page">
  <h1 class="sr-only">Dark-sky stargazing map, Scotland viewing windows</h1>

  <StarsMap
    sites={mapSites}
    {activeNights}
    {nowMs}
    {mode}
    darknessMode={darkness}
    heatVisible={heatOn}
  />

  <!-- Floating header: breadcrumb + headline stats, top-left clear of the map
       chrome (mirrors the hikes control head). -->
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
          <span class="crumb-name">stars</span>
        </nav>
        {#if mode === "historical"}
          <p class="stats">
            {histCount}
            {histCount === 1 ? "site" : "sites"} with history {historyScope}
          </p>
        {:else}
          <p class="stats">
            {count} dark-sky sites{#if topClearDark != null}
              &middot; best {topClearDark} clear-dark hrs{/if}{#if agoLabel}
              &middot; updated {agoLabel} ago{/if}
          </p>
        {/if}
      </div>
    </div>

    <!-- Mode toggle: LIVE forecast vs HISTORICAL clear-dark hours (stars v2). -->
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

      <!-- Summer twilight disclaimer: for ~7 weeks each midsummer Scotland gets
           no astronomical darkness, so the live layer falls back to the darkest
           twilight windows (down to -10 deg) and says so. -->
      {#if darkness === "none"}
        <div class="panel disclaimer" role="status">
          No usable stargazing windows in Scotland this week: right now the
          summer sky never gets dark enough, even for twilight. Astronomical
          darkness returns by August.
        </div>
      {:else if darkness === "twilight"}
        <div class="panel disclaimer" role="status">
          No astronomical darkness in Scotland right now (it returns by August).
          Showing the darkest twilight windows instead, best in the south.
        </div>
      {/if}

      {#if count === 0 && darkness === "astronomical"}
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
          <span class="filter-current">{monthSummary}</span>
          <span class="filter-caret" class:open={monthOpen} aria-hidden="true"
            >&#9662;</span
          >
        </button>
        {#if monthOpen}
          <div class="month-chips">
            <button
              type="button"
              class="month-chip month-chip-all"
              class:is-off={selectedMonth !== ALL_YEAR}
              aria-pressed={selectedMonth === ALL_YEAR}
              onclick={() => selectMonth(ALL_YEAR)}
            >
              All year
            </button>
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
          Loading history&hellip;
        </div>
      {:else if histReady && histCount === 0}
        <div class="panel empty-state" role="status">
          No clear dark hours {historyScope}. The seasonal baseline comes from
          the ERA5 reanalysis backfill.
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

  /* Summer twilight disclaimer: same mono read as the empty state, lifted with a
     thick accent left edge so it registers as a heads-up, not an error. */
  .disclaimer {
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.5;
    letter-spacing: 0.02em;
    color: var(--ink);
    border-left-width: 8px;
    border-left-color: var(--accent);
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

  /* The night reset and the "All year" option each span the full width above
     their per-chip grid. */
  .night-chip-all,
  .month-chip-all {
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
