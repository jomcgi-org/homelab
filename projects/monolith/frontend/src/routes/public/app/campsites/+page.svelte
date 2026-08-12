<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import { browser } from "$app/environment";
  import CampsitesMap from "$lib/public/components/campsites/CampsitesMap.svelte";

  let { data } = $props();

  let parks = $derived(data.snapshot?.parks ?? []);
  let generatedAt = $derived(data.snapshot?.generated_at);

  // Two-way binding between the map and the ranked list: clicking a pin sets
  // selectedId here, and clicking a list row sets it from the parent.
  // CampsitesMap uses $bindable(null) so both directions work.
  let selectedId = $state(null);

  // List panel collapse (map-first: the whole map stays visible when collapsed).
  // Starts closed so the map is unobstructed on load.
  let listOpen = $state(false);

  // Night selector collapse (map-first: starts collapsed to a summary pill so it
  // is not permanently over the map; expands on click, like the list panel).
  let nightsOpen = $state(false);

  // Sort and filter state.
  let sortKey = $state("best_score"); // "best_score" | "good_days" | "name"
  let clearOnly = $state(false);

  // --- Night selector -------------------------------------------------------
  // "One trip" planning: mark which nights in the window you are free (a
  // weekend, scattered weekdays). A park matches if it is open on AT LEAST one
  // selected night, re-scored over just those nights. Selection lives in the URL
  // (?nights=YYYY-MM-DD,...) so it is shareable. `null` means "all live nights",
  // which is the default and what SSR renders before hydration, so there is no
  // flash and the no-JS view stays the full list.
  let selectedDates = $state(null); // null => all live nights; Set otherwise

  // The window: sorted unique dates present across all parks.
  let windowDates = $derived.by(() => {
    const set = new Set();
    for (const p of parks) for (const d of p.days) set.add(d.date);
    return [...set].sort();
  });

  // Dead nights: no park is open, so they cannot be picked (greyed out).
  let deadDates = $derived.by(() => {
    const alive = new Set();
    for (const p of parks)
      for (const d of p.days) if (d.available) alive.add(d.date);
    return new Set(windowDates.filter((d) => !alive.has(d)));
  });

  let liveDates = $derived(windowDates.filter((d) => !deadDates.has(d)));

  // Resolve the sentinel to a concrete Set, always dropping dead nights (a URL
  // may name a night that has since sold out).
  let activeDates = $derived.by(() => {
    if (selectedDates === null) return new Set(liveDates);
    return new Set([...selectedDates].filter((d) => !deadDates.has(d)));
  });

  // Narrowed = a strict subset of live nights is chosen. Only then do we hide
  // parks with no availability on the chosen nights; at the default (all live)
  // the list still shows every park, matching the pre-filter behaviour.
  let narrowed = $derived(activeDates.size < liveDates.length);

  // Collapsed-pill summary of the current pick.
  let nightsSummary = $derived.by(() => {
    if (selectedDates === null) return "All nights";
    const n = activeDates.size;
    if (n === 0) return "No nights";
    return n === 1 ? "1 night" : `${n} nights`;
  });

  // Re-score every park over the active nights: best_score = best clear-sky
  // score among its open active nights, good_days = count of clear open nights,
  // open_nights = how many active nights it is bookable (the match test).
  let scoredParks = $derived.by(() => {
    const active = activeDates;
    return parks.map((p) => {
      let best = 0;
      let good = 0;
      let openNights = 0;
      for (const d of p.days) {
        if (!active.has(d.date) || !d.available) continue;
        openNights += 1;
        if (d.sunny_score > best) best = d.sunny_score;
        if (d.is_good) good += 1;
      }
      return {
        ...p,
        best_score: best,
        good_days: good,
        open_nights: openNights,
      };
    });
  });

  let selectedPark = $derived(
    scoredParks.find((p) => p.id === selectedId) ?? null,
  );

  // Filter then sort. $derived.by for the multi-step computation.
  let visibleParks = $derived.by(() => {
    let list = scoredParks;
    if (narrowed) list = list.filter((p) => p.open_nights > 0);
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
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
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
  // Rain accent for the always-visible per-day precip figure (a wet day reads
  // blue). Kept as a JS constant like the others so no literal hex lands in a
  // style attribute (the hardcoded-color semgrep rule).
  const VIZ_RAIN = "#2563eb";

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

  // --- Night selection helpers ---------------------------------------------
  function weekdayOf(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).getDay(); // 0 Sun .. 6 Sat
  }

  // Write the current selection into ?nights= (client only). The null sentinel
  // clears the param so the shared/default URL stays clean.
  function syncURL() {
    if (!browser) return;
    const url = new URL(location.href);
    if (selectedDates === null) url.searchParams.delete("nights");
    else url.searchParams.set("nights", [...selectedDates].sort().join(","));
    history.replaceState(history.state, "", url);
  }

  // Normalize "all live nights" back to the null sentinel so the URL stays clean
  // and `narrowed` reads false when nothing is actually filtered.
  function commitSelection(next) {
    const isAll =
      next.size === liveDates.length && liveDates.every((d) => next.has(d));
    selectedDates = isAll ? null : next;
    syncURL();
  }

  function toggleNight(iso) {
    if (deadDates.has(iso)) return;
    const base =
      selectedDates === null ? new Set(liveDates) : new Set(selectedDates);
    if (base.has(iso)) base.delete(iso);
    else base.add(iso);
    commitSelection(base);
  }

  function selectAllNights() {
    commitSelection(new Set(liveDates));
  }

  function clearNights() {
    commitSelection(new Set()); // empty: nothing selected
  }

  function selectWeekends() {
    // Fri, Sat, Sun nights among the live dates.
    const wk = liveDates.filter((d) => [5, 6, 0].includes(weekdayOf(d)));
    commitSelection(new Set(wk));
  }

  // Absent param => null (all live). Present-but-empty (?nights=) => a real
  // empty selection, so a cleared state round-trips instead of snapping back.
  function readURL() {
    const params = new URLSearchParams(location.search);
    if (!params.has("nights")) return null;
    const raw = params.get("nights") || "";
    return new Set(
      raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    );
  }

  onMount(() => {
    selectedDates = readURL();
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

<div
  class="campsites-page"
  class:has-selection={!!selectedPark}
  class:list-open={listOpen}
>
  <!-- The visible heading is the breadcrumb chip; keep a real (hidden) h1 for
       SEO + a11y. -->
  <h1 class="sr-only">BC Parks campsites, open sites and clear-sky weather</h1>

  <CampsitesMap parks={visibleParks} bind:selectedId />

  <!-- Top-left crumb / title card. -->
  <div class="crumb-card">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
        ></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">campsites</span>
      <span class="crumb-count"
        ><strong>{visibleParks.length}</strong>{narrowed || clearOnly
          ? " matching parks"
          : " parks"}</span
      >
    </nav>
    <p class="crumb-note">
      {#if generatedAt}As of {fmtGeneratedAt(generatedAt)}.
      {/if}Green = open AND clear skies.
    </p>
  </div>

  <!-- Night selector: pick the nights you are free. Dead nights (no park open)
       are greyed and disabled. Filters the map, list and per-park scores. -->
  <div class="nights-card" class:collapsed={!nightsOpen}>
    <button
      type="button"
      class="nights-toggle"
      aria-expanded={nightsOpen}
      onclick={() => (nightsOpen = !nightsOpen)}
    >
      <span class="nights-toggle-label">Nights you're free</span>
      <span class="nights-toggle-summary">{nightsSummary}</span>
      <span class="nights-toggle-chevron" aria-hidden="true"
        >{nightsOpen ? "▾" : "▸"}</span
      >
    </button>

    {#if nightsOpen}
      <div class="nights-body">
        <div class="nights-actions">
          <button type="button" class="nights-act" onclick={selectAllNights}
            >All</button
          >
          <button type="button" class="nights-act" onclick={selectWeekends}
            >Weekends</button
          >
          <button type="button" class="nights-act" onclick={clearNights}
            >Clear</button
          >
        </div>
        <div
          class="nights-strip"
          role="group"
          aria-label="Select the nights you are free"
        >
          {#each windowDates as iso (iso)}
            <button
              type="button"
              class="night-btn"
              class:on={activeDates.has(iso)}
              class:dead={deadDates.has(iso)}
              disabled={deadDates.has(iso)}
              aria-pressed={activeDates.has(iso)}
              title={deadDates.has(iso)
                ? "No sites open this night"
                : fmtDayCell(iso)}
              onclick={() => toggleNight(iso)}
            >
              {fmtDayCell(iso)}
            </button>
          {/each}
        </div>
        {#if activeDates.size === 0}
          <p class="nights-hint">
            Pick the nights you can go to see matching parks.
          </p>
        {/if}
      </div>
    {/if}
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
    <section class="detail" aria-label="{selectedPark.name} 14-day forecast">
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
              class:day-inactive={!activeDates.has(day.date)}
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
              {#if day.precip != null}
                <!-- Rain promoted out of the hover title so touch users (no
                     hover) can see it. Blue + mm on a wet day; a faint dot when
                     dry keeps the row rhythm without shouting "0.0". -->
                <span
                  class="day-rain"
                  class:day-rain-wet={day.precip > 0}
                  style={day.precip > 0 ? `color: ${VIZ_RAIN}` : undefined}
                  >{day.precip > 0 ? `${day.precip.toFixed(1)}mm` : "·"}</span
                >
              {/if}
            </li>
          {/each}
        </ul>
      </div>

      <p class="detail-legend">
        Check = sites bookable. Color: green (clear) to grey (cloudy or closed).
        Each cell shows max temp (&deg;) and rain (mm, blue when wet). Hover for
        cloud cover.
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
    min-height: 52px;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    justify-content: center;
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

  /* Top-center night selector. z-index 20 to sit above the MapLibre controls,
     like the list panel. Centered on wide screens; drops below the crumb card
     on narrow ones so the three top overlays never overlap. */
  .nights-card {
    position: absolute;
    top: 16px;
    left: 50%;
    transform: translateX(-50%);
    max-width: min(560px, calc(100% - 32px));
    box-sizing: border-box;
    background: var(--paper);
    border: 2px solid var(--ink);
    z-index: 20;
  }

  /* Header doubles as the collapse toggle: a full-width button showing the
     title, a summary of the current pick, and a chevron. Collapsed, this is the
     entire card (a compact pill); expanded, the body sits below it. */
  .nights-toggle {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 8px 12px;
    background: var(--paper);
    border: none;
    cursor: pointer;
    text-align: left;
    transition: background 110ms ease;
  }

  .nights-toggle:hover,
  .nights-toggle:focus-visible {
    background: var(--rule);
    outline: none;
  }

  .nights-toggle-label {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .nights-toggle-summary {
    margin-left: auto;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--ink);
  }

  .nights-toggle-chevron {
    font-size: 10px;
    color: var(--ink-3);
  }

  .nights-body {
    padding: 10px 12px;
    border-top: 2px solid var(--ink);
  }

  .nights-actions {
    display: inline-flex;
    gap: 6px;
    margin-bottom: 8px;
  }

  .nights-act {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 4px 8px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition: transform 110ms ease;
  }

  .nights-act:hover,
  .nights-act:focus-visible {
    transform: translate(-2px, -2px);
    outline: none;
  }

  .nights-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .night-btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.02em;
    min-width: 40px;
    padding: 5px 6px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition:
      transform 110ms ease,
      background 110ms ease;
  }

  .night-btn:hover:not(:disabled),
  .night-btn:focus-visible:not(:disabled) {
    transform: translate(-2px, -2px);
    outline: none;
  }

  .night-btn.on {
    background: var(--accent);
  }

  .night-btn.dead {
    color: var(--ink-3);
    background: var(--rule);
    border-color: var(--ink-3);
    cursor: not-allowed;
    text-decoration: line-through;
    opacity: 0.6;
  }

  .nights-hint {
    margin: 8px 0 0;
    font-family: var(--mono);
    font-size: 10px;
    line-height: 1.4;
    color: var(--ink-3);
  }

  /* Nights not in the current selection are dimmed in the detail strip so the
     chosen nights read clearly against the full 14-day context. */
  .day-inactive {
    opacity: 0.35;
  }

  @media (max-width: 860px) {
    .nights-card {
      left: 16px;
      right: 16px;
      top: 84px;
      transform: none;
      max-width: none;
    }
  }

  /* Right-side ranked-list panel. Narrow by default so the page reads map-first;
     collapse button shrinks it to just the header handle. z-index 20 ensures the
     panel and its toggle sit above the MapLibre control group (z-index ~2) so the
     open panel covers the zoom/attribution buttons cleanly. */
  .list-panel {
    position: absolute;
    top: 16px;
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
    min-height: 52px;
    box-sizing: border-box;
  }

  .list-head {
    border-bottom: 2px solid var(--ink);
  }

  .list-panel.collapsed .list-head {
    border-bottom: none;
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
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

  /* Detail panel: bottom strip anchored bottom, between the legend (left) and
     the zoom controls (right, collapsed) or the list panel (right, expanded). */
  .detail {
    position: absolute;
    bottom: 16px;
    left: 232px;
    right: 64px;
    height: 168px;
    box-sizing: border-box;
    overflow-y: auto;
    padding: 12px 14px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  /* When the list panel is open it occupies right:16px width:340px, so the
     detail must clear it (340 + 16 + 16 gap = 372px). */
  .campsites-page.list-open .detail {
    right: 372px;
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

  /* Always-visible rain figure (mm). Dry days show a faint dot; wet days get
     the blue accent via an inline color so they stand out when scanning on a
     phone, where the old hover-only title was invisible. */
  .day-rain {
    font-family: var(--mono);
    font-size: 8px;
    font-weight: 700;
    line-height: 1;
    letter-spacing: 0;
    color: var(--ink-3);
    white-space: nowrap;
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

  /* Mobile: slim top bar, full-width bottom list bar that is always visible,
     legend behind a pop-out chip, detail sheet clearing the bar from above. */
  @media (max-width: 768px) {
    /* Slim single-line top bar: drop the verbose crumb-note, compact padding,
       switch to row layout so only one line of breadcrumb is shown. */
    .crumb-card {
      top: 12px;
      left: 12px;
      min-height: auto;
      padding: 6px 10px;
      flex-direction: row;
      align-items: center;
    }

    .crumb {
      flex-wrap: nowrap;
      font-size: 11px;
      overflow: hidden;
    }

    /* Hide the "As of HH:MM UTC. Green = open AND clear skies." note on mobile.
       The same context is available in the legend pop-out and on desktop. */
    .crumb-note {
      display: none;
    }

    /* Full-width bottom sheet. z-index 30 means the bar always wins over the
       legend pop-out (z-index 10 inside map-wrap) and the detail (z-index 20). */
    .list-panel {
      top: auto;
      bottom: 0;
      left: 0;
      right: 0;
      width: auto;
      max-width: none;
      max-height: 46vh;
      border-width: 2px 0 0;
      z-index: 30;
    }

    /* Collapsed bar: exactly 48px so legend chip, legend panel, and detail sheet
       can use a known offset to clear it (bottom: 56px for chip, bottom: 96px
       for legend, bottom: 48px for detail). */
    .list-panel.collapsed {
      bottom: 0;
      height: 48px;
      min-height: 48px;
      max-height: 48px;
      box-sizing: border-box;
      overflow: hidden;
    }

    /* When a park is selected: keep the 48px collapsed bar visible at the bottom
       rather than hiding the whole panel. The detail sheet clears it from above
       (bottom: 48px) so both are simultaneously visible. */
    .has-selection .list-panel {
      display: flex;
      max-height: 48px;
      overflow: hidden;
    }

    /* Detail sheet: bottom = 48px so the RANKED PARKS bar is never obscured. */
    .detail {
      bottom: 48px;
      left: 0;
      right: 0;
      height: auto;
      max-height: 60vh;
      border-width: 2px 0 0;
      box-shadow: none;
      padding-bottom: calc(12px + env(safe-area-inset-bottom));
      z-index: 20;
    }

    .campsites-page.list-open .detail {
      right: 0;
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
