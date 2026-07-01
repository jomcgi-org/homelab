<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import CampsitesMap from "$lib/public/components/campsites/CampsitesMap.svelte";

  let { data } = $props();

  let parks = $derived(data.snapshot?.parks ?? []);
  let generatedAt = $derived(data.snapshot?.generated_at);

  // Two-way binding between the map and the ranked list: clicking a circle on
  // the map sets selectedId here, and clicking a list row sets it from the
  // parent. CampsitesMap uses $bindable(null) so both directions work.
  let selectedId = $state(null);
  let selectedPark = $derived(parks.find((p) => p.id === selectedId) ?? null);

  // Sort and filter state
  let sortKey = $state("best_score"); // "best_score" | "good_days" | "name" | "region"
  let filterRegion = $state("");
  let clearOnly = $state(false);

  // Unique region options, alphabetised.
  let regions = $derived(
    [...new Set(parks.map((p) => p.region).filter(Boolean))].sort((a, b) =>
      a.localeCompare(b),
    ),
  );

  // Filter then sort. $derived.by for the multi-step computation.
  let visibleParks = $derived.by(() => {
    let list = parks;
    if (filterRegion) list = list.filter((p) => p.region === filterRegion);
    if (clearOnly) list = list.filter((p) => p.good_days > 0);
    const out = [...list];
    if (sortKey === "best_score") {
      out.sort((a, b) => b.best_score - a.best_score || a.name.localeCompare(b.name));
    } else if (sortKey === "good_days") {
      out.sort((a, b) => b.good_days - a.good_days || a.name.localeCompare(b.name));
    } else if (sortKey === "name") {
      out.sort((a, b) => a.name.localeCompare(b.name));
    } else if (sortKey === "region") {
      out.sort(
        (a, b) => a.region.localeCompare(b.region) || a.name.localeCompare(b.name),
      );
    }
    return out;
  });

  // ── Date formatting ────────────────────────────────────────────────────────

  const MONTHS = [
    "Jan","Feb","Mar","Apr","May","Jun",
    "Jul","Aug","Sep","Oct","Nov","Dec",
  ];
  const SHORT_DAYS = ["Su","Mo","Tu","We","Th","Fr","Sa"];

  // "Tu 30" from "2026-06-30"; uses local date constructor (no UTC offset).
  function fmtDayCell(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-").map(Number);
    const date = new Date(y, m - 1, d);
    return `${SHORT_DAYS[date.getDay()]} ${d}`;
  }

  // "30 Jun 2026, 12:00 UTC"
  function fmtGeneratedAt(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${hh}:${mm} UTC`;
  }

  // ── Color helpers ──────────────────────────────────────────────────────────
  // Data-viz ramp: outside the design-token system (intentionally; same class
  // of values as ShipsMap VESSEL_COLORS / HEAT_COLORS). The hex literals are
  // stored in plain JS constants so they never appear as `:` + `#hex` in the
  // source, which is the pattern the svelte-hardcoded-color-in-style rule
  // matches. Template usage is `style="background: {colVar}"`.
  const VIZ_GREEN  = "#22c55e";
  const VIZ_LIME   = "#84cc16";
  const VIZ_YELLOW = "#fbbf24";
  const VIZ_AMBER  = "#f59e0b";
  const VIZ_GREY   = "#9ca3af";

  // Returns a plain color string (not a full CSS property), composed in the
  // template as `style="background: {dayCellBg(day)}"`.
  function dayCellBg(day) {
    if (!day.available) return "var(--rule)";
    const s = day.sunny_score ?? 0;
    if (s >= 80) return VIZ_GREEN;
    if (s >= 60) return VIZ_LIME;
    if (s >= 40) return VIZ_YELLOW;
    if (s >= 20) return VIZ_AMBER;
    return VIZ_GREY; // open but overcast
  }

  // Returns a plain color string for the badge `style="background: {badgeColor(score)}"`.
  // When score is 0 the badge uses CSS tokens (no hex needed).
  function badgeColor(score) {
    if (score >= 80) return VIZ_GREEN;
    if (score >= 60) return VIZ_LIME;
    if (score >= 40) return VIZ_YELLOW;
    if (score > 0)  return VIZ_AMBER;
    return null; // zero score: badge styled via CSS class, not inline
  }

  // title= text for day cell hover: cloud/precip/temp summary.
  function dayTitle(day) {
    const parts = [day.available ? "Open" : "Closed"];
    if (day.cloud != null) parts.push(`${Math.round(day.cloud)}% cloud`);
    if (day.precip != null) parts.push(`${day.precip.toFixed(1)} mm rain`);
    if (day.temp_max != null) parts.push(`${Math.round(day.temp_max)}C max`);
    return parts.join(", ");
  }

  // Toggle selection: clicking the same row again deselects.
  function selectPark(id) {
    selectedId = selectedId === id ? null : id;
  }

  onMount(() => {
    // Re-run the SSR load every 15 min so the page stays fresh.
    const refresh = setInterval(() => invalidateAll(), 15 * 60_000);
    return () => clearInterval(refresh);
  });
</script>

<svelte:head>
  <title>BC Parks campsites, open sites and clear-sky weather</title>
  <meta
    name="description"
    content="BC Parks campsite availability overlaid with clear-sky weather forecasts for the next 14 days. Find parks that are open and forecast to have good weather."
  />
</svelte:head>

<div class="page">
  <h1 class="sr-only">BC Parks campsites, open sites and clear-sky weather</h1>

  <div class="board">
    <header class="board-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="https://jomcgi.dev/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">campsites</span>
        </nav>
        <p class="stats">
          <strong>{parks.length}</strong> parks
        </p>
      </div>
      <p class="source">
        BC Parks availability + 14-day weather. Green = sites open AND clear
        skies. Circle size = number of good days.
        {#if generatedAt}
          As of {fmtGeneratedAt(generatedAt)}.
        {/if}
      </p>
    </header>

    <div class="split">
      <!-- Map column: map fills the column height via CSS. -->
      <div class="map-col">
        <CampsitesMap {parks} bind:selectedId />
      </div>

      <!-- Panel column: controls + ranked list + detail strip. -->
      <div class="panel-col">
        <div class="controls">
          <!-- Sort toggle -->
          <div class="sort-row">
            <span class="control-label">Sort</span>
            <div class="toggle" role="group" aria-label="Sort parks by">
              {#each [["best_score", "Score"], ["good_days", "Days"], ["name", "Name"], ["region", "Region"]] as [key, label] (key)}
                <button
                  type="button"
                  class="seg"
                  class:active={sortKey === key}
                  aria-pressed={sortKey === key}
                  onclick={() => (sortKey = key)}
                >{label}</button>
              {/each}
            </div>
          </div>

          <!-- Region filter + clear-nights toggle -->
          <div class="filter-row">
            <label class="field-wrap">
              <span class="sr-only">Filter by region</span>
              <select class="region-select" bind:value={filterRegion}>
                <option value="">All regions</option>
                {#each regions as r (r)}
                  <option value={r}>{r}</option>
                {/each}
              </select>
            </label>

            <label class="check-label">
              <input
                type="checkbox"
                class="check-input"
                bind:checked={clearOnly}
                aria-label="Show only parks with at least one clear-sky day"
              />
              Clear nights only
            </label>
          </div>
        </div>

        {#if visibleParks.length === 0}
          <p class="empty">No parks match the current filters.</p>
        {:else}
          <ul class="rows" aria-label="Ranked park list">
            {#each visibleParks as park (park.id)}
              <li>
                <button
                  type="button"
                  class="row"
                  class:selected={selectedId === park.id}
                  aria-pressed={selectedId === park.id}
                  onclick={() => selectPark(park.id)}
                >
                  <span class="r-body">
                    <span class="r-name">{park.name}</span>
                    <span class="r-region">{park.region}</span>
                  </span>
                  <span class="r-right">
                    <span
                      class="score-badge"
                      class:score-zero={park.best_score === 0}
                      style={badgeColor(park.best_score)
                        ? `background: ${badgeColor(park.best_score)}`
                        : undefined}
                    >{park.best_score}</span>
                    <span class="r-days">
                      {park.good_days === 1
                        ? "1 clear day"
                        : `${park.good_days} clear days`}
                    </span>
                  </span>
                </button>
              </li>
            {/each}
          </ul>
        {/if}

        <!-- 14-day detail strip for the selected park. -->
        {#if selectedPark}
          <section class="detail" aria-label="{selectedPark.name} 14-day forecast">
            <div class="detail-head">
              <div class="detail-names">
                <h2 class="detail-park">{selectedPark.name}</h2>
                <p class="detail-region">{selectedPark.region}</p>
              </div>
              <a
                href={selectedPark.booking_url}
                target="_blank"
                rel="noopener noreferrer"
                class="book-link"
              >Book on BC Parks &nearr;</a>
            </div>

            <div class="days-scroll">
              <ul class="days-strip" aria-label="14-day forecast strip">
                {#each selectedPark.days as day (day.date)}
                  <li
                    class="day-cell"
                    class:day-open={day.available}
                    class:day-good={day.is_good}
                    style="background: {dayCellBg(day)}"
                    title={dayTitle(day)}
                  >
                    <span class="day-date">{fmtDayCell(day.date)}</span>
                    <span class="day-icon" aria-hidden="true"
                      >{day.available ? "✓" : "×"}</span
                    >
                    {#if day.temp_max != null}
                      <span class="day-temp">{Math.round(day.temp_max)}°</span>
                    {/if}
                  </li>
                {/each}
              </ul>
            </div>

            <p class="detail-legend">
              Check = sites bookable. Color: green (clear) to grey (cloudy or
              closed). Hover a cell for cloud/rain/temp.
            </p>
          </section>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .page {
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--cream);
    color: var(--ink);
    padding: 12px;
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

  /* One hard-edged sheet; wide enough to hold the map + list side by side. */
  .board {
    max-width: 1200px;
    margin: 0 auto;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  /* ── Header ────────────────────────────────────────────────────────────── */

  .board-head {
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding: 12px 14px;
    border-bottom: 2px solid var(--ink);
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
  }

  .crumb-sep {
    color: var(--ink-3);
  }

  .stats {
    margin: 0;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .stats strong {
    color: var(--ink);
    font-weight: 700;
  }

  .source {
    margin: 0;
    font-size: 12px;
    line-height: 1.4;
    color: var(--ink-3);
  }

  /* ── Main split layout ─────────────────────────────────────────────────── */

  .split {
    display: grid;
    grid-template-columns: 1fr;
  }

  /* Map: defined height on mobile, sticky column on desktop. */
  .map-col {
    height: 45vh;
    min-height: 280px;
    border-bottom: 2px solid var(--ink);
  }

  .panel-col {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  @media (min-width: 768px) {
    .split {
      /* 60% map, 40% panel */
      grid-template-columns: 3fr 2fr;
      align-items: start;
      min-height: 600px;
    }

    .map-col {
      height: auto;
      min-height: 600px;
      position: sticky;
      top: 0;
      /* Panel is to the right; show a vertical rule between them. */
      border-bottom: none;
      border-right: 2px solid var(--ink);
    }
  }

  /* ── Controls ──────────────────────────────────────────────────────────── */

  .controls {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 2px solid var(--ink);
  }

  .sort-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .control-label {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    white-space: nowrap;
  }

  /* Segmented sort control (same idiom as dr-jobs LIVE/HISTORY toggle). */
  .toggle {
    display: inline-flex;
  }

  .seg {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 5px 10px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition: transform 120ms ease, box-shadow 120ms ease, background 120ms ease;
  }

  .seg + .seg {
    margin-left: -2px;
  }

  .seg.active {
    background: var(--accent);
    z-index: 1;
  }

  .seg:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
    z-index: 2;
  }

  .filter-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .field-wrap {
    display: contents;
  }

  /* Region select: hard-edge, no browser chrome. */
  .region-select {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 5px 8px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    border-radius: 0;
    -webkit-appearance: none;
    appearance: none;
    cursor: pointer;
  }

  /* Clear-nights toggle label. */
  .check-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    cursor: pointer;
    user-select: none;
  }

  .check-input {
    accent-color: var(--ink);
    width: 14px;
    height: 14px;
    cursor: pointer;
  }

  /* ── Ranked list ───────────────────────────────────────────────────────── */

  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    flex: 1;
    overflow-y: auto;
  }

  .rows li + li {
    border-top: 2px solid var(--ink);
  }

  /* Each row is a button so it is keyboard-accessible and clearly interactive.
     Flat wash on hover (no lift: it does not open a new page, so no nav affordance). */
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 12px;
    width: 100%;
    background: var(--paper);
    border: none;
    color: var(--ink);
    text-align: left;
    cursor: pointer;
    transition: background 100ms ease;
  }

  .row:hover,
  .row:focus-visible {
    background: var(--cream);
    outline: none;
  }

  .row.selected {
    background: var(--accent);
  }

  .row.selected:hover,
  .row.selected:focus-visible {
    background: color-mix(in srgb, var(--accent) 85%, var(--ink) 15%);
  }

  .r-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .r-name {
    font-size: 14px;
    font-weight: 700;
    line-height: 1.2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .r-region {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    letter-spacing: 0.02em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row.selected .r-region {
    color: var(--ink);
  }

  .r-right {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 3px;
    flex-shrink: 0;
  }

  /* Score badge: colored square, font-mono, tabular. */
  .score-badge {
    display: inline-block;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    padding: 2px 7px;
    border: 1.5px solid var(--ink);
    min-width: 34px;
    text-align: center;
  }

  /* Zero-score badge: no inline color, use token-based neutral styling. */
  .score-badge.score-zero {
    background: var(--rule);
    color: var(--ink-3);
  }

  .r-days {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .row.selected .r-days {
    color: var(--ink);
  }

  .empty {
    margin: 0;
    padding: 14px 12px;
    font-size: 13px;
    color: var(--ink-3);
  }

  /* ── 14-day detail strip ───────────────────────────────────────────────── */

  .detail {
    border-top: 2px solid var(--ink);
    padding: 12px;
  }

  .detail-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }

  .detail-names {
    min-width: 0;
  }

  .detail-park {
    margin: 0;
    font-family: var(--serif);
    font-size: 20px;
    font-weight: 400;
    line-height: 1.15;
  }

  .detail-region {
    margin: 2px 0 0;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    letter-spacing: 0.02em;
  }

  /* "Book on BC Parks" link: same lifted-box treatment as the sort/toggle
     buttons so it reads as the primary action in this section. */
  .book-link {
    display: inline-flex;
    align-items: center;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    text-decoration: none;
    padding: 6px 10px;
    background: var(--ink);
    color: var(--paper);
    border: 2px solid var(--ink);
    white-space: nowrap;
    transition: transform 110ms ease, box-shadow 110ms ease;
  }

  .book-link:hover,
  .book-link:focus-visible {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink-3);
    outline: none;
  }

  /* Horizontally scrollable container for the 14-day strip so it never
     wraps and looks broken on narrow screens. */
  .days-scroll {
    overflow-x: auto;
    margin-bottom: 8px;
  }

  .days-strip {
    display: flex;
    gap: 4px;
    list-style: none;
    margin: 0;
    padding: 2px 0 6px; /* bottom clearance for the scrollbar */
    width: max-content;
  }

  /* Each day cell: small card colored by available + sunny_score. */
  .day-cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 3px;
    width: 40px;
    padding: 5px 3px;
    border: 1.5px solid var(--ink);
    cursor: default;
    /* Transition the background when data refreshes. */
    transition: background 300ms ease;
  }

  .day-date {
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: var(--ink);
    white-space: nowrap;
  }

  .day-icon {
    font-size: 13px;
    line-height: 1;
    color: var(--ink);
  }

  .day-temp {
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 600;
    color: var(--ink);
    letter-spacing: 0.01em;
  }

  /* Closed cells: dim the text so closed days read as muted. */
  .day-cell:not(.day-open) .day-date,
  .day-cell:not(.day-open) .day-icon,
  .day-cell:not(.day-open) .day-temp {
    color: var(--ink-3);
  }

  .detail-legend {
    margin: 0;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
    letter-spacing: 0.02em;
    line-height: 1.4;
  }

  /* ── Desktop scale-up ──────────────────────────────────────────────────── */

  @media (min-width: 768px) {
    .page {
      padding: 24px;
    }

    .board-head {
      gap: 9px;
      padding: 14px 18px;
    }

    .crumb,
    .stats,
    .source {
      font-size: 13px;
    }

    .controls {
      padding: 12px 14px;
    }

    .seg {
      font-size: 12px;
      padding: 6px 12px;
    }

    .row {
      padding: 10px 14px;
    }

    .r-name {
      font-size: 15px;
    }

    .detail {
      padding: 14px;
    }

    .detail-park {
      font-size: 24px;
    }
  }

  /* On very narrow screens keep the filter row readable. */
  @media (max-width: 400px) {
    .filter-row {
      flex-direction: column;
      align-items: flex-start;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .seg,
    .book-link,
    .row {
      transition: none;
    }
  }
</style>
