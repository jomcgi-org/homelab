<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import CampsitesMap from "$lib/public/components/campsites/CampsitesMap.svelte";

  let { data } = $props();

  let parks = $derived(data.snapshot?.parks ?? []);
  let generatedAt = $derived(data.snapshot?.generated_at);

  // Two-way binding between the map and the ranked list: clicking a pin sets
  // selectedId here, and clicking a list row sets it from the parent.
  // CampsitesMap uses $bindable(null) so both directions work.
  let selectedId = $state(null);
  let selectedPark = $derived(parks.find((p) => p.id === selectedId) ?? null);

  // List panel collapse (map-first: the whole map stays visible when collapsed).
  // Starts closed so the map is unobstructed on load.
  let listOpen = $state(false);

  // Sort and filter state.
  let sortKey = $state("best_score"); // "best_score" | "good_days" | "name"
  let clearOnly = $state(false);

  // Filter then sort. $derived.by for the multi-step computation.
  let visibleParks = $derived.by(() => {
    let list = parks;
    if (clearOnly) list = list.filter((p) => p.good_days > 0);
    const out = [...list];
    if (sortKey === "best_score") {
      out.sort(
        (a, b) => b.best_score - a.best_score || a.name.localeCompare(b.name),
      );
    } else if (sortKey === "good_days") {
      out.sort(
        (a, b) => b.good_days - a.good_days || a.name.localeCompare(b.name),
      );
    } else if (sortKey === "name") {
      out.sort((a, b) => a.name.localeCompare(b.name));
    }
    return out;
  });

  // Date formatting.

  const MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  const SHORT_DAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

  // "Tu 30" from "2026-06-30"; local date constructor (no UTC offset).
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

  // Color helpers. Data-viz ramp hexes live in plain JS constants so they never
  // appear as `:` + `#hex` inside a style attribute (the pattern the
  // svelte-hardcoded-color-in-style rule matches). Template usage is
  // `style="background: {colVar}"`.
  const VIZ_GREEN = "#22c55e";
  const VIZ_LIME = "#84cc16";
  const VIZ_YELLOW = "#fbbf24";
  const VIZ_AMBER = "#f59e0b";
  const VIZ_GREY = "#9ca3af";

  function dayCellBg(day) {
    if (!day.available) return "var(--rule)";
    const s = day.sunny_score ?? 0;
    if (s >= 80) return VIZ_GREEN;
    if (s >= 60) return VIZ_LIME;
    if (s >= 40) return VIZ_YELLOW;
    if (s >= 20) return VIZ_AMBER;
    return VIZ_GREY; // open but overcast
  }

  function badgeColor(score) {
    if (score >= 80) return VIZ_GREEN;
    if (score >= 60) return VIZ_LIME;
    if (score >= 40) return VIZ_YELLOW;
    if (score > 0) return VIZ_AMBER;
    return null; // zero score: badge styled via CSS class, not inline
  }

  // title= text for a day cell: cloud/precip/temp summary.
  function dayTitle(day) {
    const parts = [day.available ? "Open" : "Closed"];
    if (day.cloud != null) parts.push(`${Math.round(day.cloud)}% cloud`);
    if (day.precip != null) parts.push(`${day.precip.toFixed(1)} mm rain`);
    if (day.temp_max != null) parts.push(`${Math.round(day.temp_max)}C max`);
    return parts.join(", ");
  }

  function daysLabel(n) {
    return n === 1 ? "1 clear day" : `${n} clear days`;
  }

  // Clicking a row selects (and pans the map via the bindable); clicking the
  // selected row again deselects.
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
    content="A full-screen map of BC Parks campsites overlaid with clear-sky weather forecasts for the next 14 days. Find parks that are open and forecast to have good weather."
  />
</svelte:head>

<div class="campsites-page" class:has-selection={!!selectedPark}>
  <!-- The visible heading is the breadcrumb chip; keep a real (hidden) h1 for
       SEO + a11y. -->
  <h1 class="sr-only">BC Parks campsites, open sites and clear-sky weather</h1>

  <CampsitesMap {parks} bind:selectedId />

  <!-- Top-left crumb / title card. -->
  <div class="crumb-card">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="https://jomcgi.dev/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">campsites</span>
      <span class="crumb-count"><strong>{parks.length}</strong> parks</span>
    </nav>
    <p class="crumb-note">
      {#if generatedAt}As of {fmtGeneratedAt(generatedAt)}. {/if}Green = open AND
      clear skies.
    </p>
  </div>

  <!-- Right-side list panel (collapsible). -->
  <aside class="list-panel" class:collapsed={!listOpen}>
    <header class="list-head">
      <div class="list-head-top">
        <p class="list-title">Ranked parks</p>
        <button
          type="button"
          class="collapse-btn"
          aria-expanded={listOpen}
          onclick={() => (listOpen = !listOpen)}
        >
          {listOpen ? "Hide" : "List"}
        </button>
      </div>

      {#if listOpen}
        <div class="controls">
          <div class="sort-row">
            <span class="control-label">Sort</span>
            <div class="toggle" role="group" aria-label="Sort parks by">
              {#each [["best_score", "Score"], ["good_days", "Days"], ["name", "Name"]] as [key, label] (key)}
                <button
                  type="button"
                  class="seg"
                  class:active={sortKey === key}
                  aria-pressed={sortKey === key}
                  onclick={() => (sortKey = key)}>{label}</button
                >
              {/each}
            </div>
          </div>

          <div class="filter-row">
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
      {/if}
    </header>

    {#if listOpen}
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
                      : undefined}>{park.best_score}</span
                  >
                  <span class="r-days">{daysLabel(park.good_days)}</span>
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </aside>

  <!-- Detail panel: bottom sheet for the selected park's 14-day forecast. -->
  {#if selectedPark}
    <section
      class="detail"
      aria-label="{selectedPark.name} 14-day forecast"
    >
      <button
        type="button"
        class="detail-close"
        onclick={() => (selectedId = null)}
        aria-label="Close park detail">&times;</button
      >
      <div class="detail-head">
        <div class="detail-names">
          <h2 class="detail-park">{selectedPark.name}</h2>
          <p class="detail-region">
            {selectedPark.region} &middot; {daysLabel(selectedPark.good_days)}
          </p>
        </div>
        {#if selectedPark.booking_url}
          <a
            href={selectedPark.booking_url}
            target="_blank"
            rel="noopener noreferrer"
            class="book-link">Book on BC Parks &nearr;</a
          >
        {/if}
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
                <span class="day-temp">{Math.round(day.temp_max)}&deg;</span>
              {/if}
            </li>
          {/each}
        </ul>
      </div>

      <p class="detail-legend">
        Check = sites bookable. Color: green (clear) to grey (cloudy or closed).
        Hover a cell for cloud, rain and temp.
      </p>
    </section>
  {/if}
</div>

<style>
  /* Full-bleed shell: the map fills the whole viewport under the global nav
     (no site nav on /app/* routes), and every panel is an absolutely-positioned
     overlay on top of it. Mirrors the ships/stars page shells. */
  .campsites-page {
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

  /* Top-left crumb / title card. */
  .crumb-card {
    position: absolute;
    top: 16px;
    left: 16px;
    max-width: calc(100% - 32px);
    padding: 8px 12px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .crumb {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
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
    display: inline-block;
    margin-left: 2px;
    font-size: 0.85em;
  }

  .crumb-sep {
    color: var(--ink-3);
  }

  .crumb-count {
    font-size: 11px;
    color: var(--ink-3);
    letter-spacing: 0.04em;
  }

  .crumb-count strong {
    color: var(--ink);
  }

  .crumb-note {
    margin: 6px 0 0;
    font-family: var(--mono);
    font-size: 10px;
    line-height: 1.4;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  /* Right-side ranked-list panel. Narrow by default so the page reads map-first;
     collapse button shrinks it to just the header handle. z-index 20 ensures the
     panel and its toggle sit above the MapLibre control group (z-index ~2) so the
     open panel covers the zoom/attribution buttons cleanly. */
  .list-panel {
    position: absolute;
    top: 64px;
    right: 16px;
    bottom: 16px;
    width: 340px;
    max-width: calc(100% - 32px);
    display: flex;
    flex-direction: column;
    background: var(--paper);
    border: 2px solid var(--ink);
    overflow: hidden;
    z-index: 20;
  }

  .list-panel.collapsed {
    bottom: auto;
  }

  .list-head {
    border-bottom: 2px solid var(--ink);
  }

  .list-panel.collapsed .list-head {
    border-bottom: none;
  }

  .list-head-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 12px;
  }

  .list-title {
    margin: 0;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .collapse-btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 4px 10px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition: transform 110ms ease;
  }

  .collapse-btn:hover,
  .collapse-btn:focus-visible {
    transform: translate(-2px, -2px);
    outline: none;
  }

  .controls {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 0 12px 10px;
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

  .toggle {
    display: inline-flex;
  }

  .seg {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 5px 9px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition:
      transform 120ms ease,
      background 120ms ease;
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
    z-index: 2;
  }

  .filter-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

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

  /* Detail panel: bottom card anchored center-bottom, clear of the legend
     (bottom-left, inside the map) and the list panel (bottom-right) on desktop. */
  .detail {
    position: absolute;
    bottom: 16px;
    left: 280px;
    right: 372px;
    max-height: 44vh;
    overflow-y: auto;
    padding: 12px 14px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .detail-close {
    position: absolute;
    top: 6px;
    right: 10px;
    background: none;
    border: none;
    font-family: var(--mono);
    font-size: 22px;
    line-height: 1;
    cursor: pointer;
    color: var(--ink);
  }

  .detail-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    margin: 0 24px 8px 0;
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
    transition: transform 110ms ease;
  }

  .book-link:hover,
  .book-link:focus-visible {
    transform: translate(-2px, -2px);
    outline: none;
  }

  .days-scroll {
    overflow-x: auto;
    margin-bottom: 8px;
  }

  .days-strip {
    display: flex;
    gap: 4px;
    list-style: none;
    margin: 0;
    padding: 2px 0 6px;
    width: max-content;
  }

  .day-cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 3px;
    flex: 0 0 40px;
    width: 40px;
    height: 64px;
    padding: 5px 3px;
    border: 1.5px solid var(--ink);
    cursor: default;
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

  /* Mobile: list + detail become bottom sheets; the map still owns the top of
     the screen (map-first). A selected park's detail takes over the sheet space,
     so the list hides while it is open to avoid stacking two bottom sheets. */
  @media (max-width: 768px) {
    .crumb-card {
      top: 12px;
      left: 12px;
    }

    .list-panel {
      top: auto;
      bottom: 0;
      left: 0;
      right: 0;
      width: auto;
      max-width: none;
      max-height: 46vh;
      border-width: 2px 0 0;
    }

    .list-panel.collapsed {
      bottom: 0;
    }

    .has-selection .list-panel {
      display: none;
    }

    .detail {
      bottom: 0;
      left: 0;
      right: 0;
      max-height: 60vh;
      border-width: 2px 0 0;
      box-shadow: none;
      padding-bottom: calc(12px + env(safe-area-inset-bottom));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .seg,
    .book-link,
    .collapse-btn,
    .row {
      transition: none;
    }
  }
</style>
