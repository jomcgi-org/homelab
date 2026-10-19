<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import { browser } from "$app/environment";
  import CampsitesMap from "$lib/public/components/campsites/CampsitesMap.svelte";
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import SiteIndex from "$lib/public/components/SiteIndex.svelte";
  import { Button } from "@homelab/design-system/components";
  import "$lib/public/styles/technical-drawing.css";
  import "$lib/public/components/campsites/campsites.css";

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

  // Night and list controls start collapsed in the sheet's control column.
  let nightsOpen = $state(false);

  // Sort and filter state.
  let sortKey = $state("best_score"); // "best_score" | "good_days" | "name"
  let clearOnly = $state(false);

  // --- Night selector -------------------------------------------------------
  // "One trip" planning: mark which nights in the window you are free (a
  // weekend, scattered weekdays). A park matches if it is open on AT LEAST one
  // selected night, re-scored over just those nights. Selection lives in the URL
  // (?nights=YYYY-MM-DD,...) so it is shareable. `null` means "all live nights",
  // which is the default SSR state. URL selections are applied on mount.
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

  // Collapsed control summary of the current pick.
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

  // Semantic weather fills follow the sheet in both schemes. The map uses
  // the same roles as solid pin colors; forecast text stays on tinted paper.
  const VIZ_GREEN = "var(--camp-clear-fill)";
  const VIZ_LIME = "var(--camp-fair-fill)";
  const VIZ_YELLOW = "var(--camp-mixed-fill)";
  const VIZ_AMBER = "var(--camp-poor-fill)";
  const VIZ_GREY = "var(--band)";
  const VIZ_RAIN = "var(--tone-gpu)";

  function dayCellBg(day) {
    if (!day.available) return "var(--band)";
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
    return n === 1 ? "1 clear night" : `${n} clear nights`;
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

{#snippet trail()}
  <Trail crumbs={[{ label: "campsites" }]} label="Breadcrumb" />
{/snippet}

<TechnicalDrawingChrome {trail} />

<main
  class="td sheet campsites-page"
  class:has-selection={!!selectedPark}
  class:list-open={listOpen}
>
  <h1 class="sr-only">BC Parks campsites, open sites and clear-sky weather</h1>
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <section class="edition crumb-card">
          <div class="ed-head">
            <span class="crumb-count"
              ><strong>{visibleParks.length}</strong>{narrowed || clearOnly
                ? " matching parks"
                : " parks"}</span
            >
          </div>
          <p class="ed-body crumb-note">
            {#if generatedAt}As of {fmtGeneratedAt(generatedAt)}.{" "}
            {/if}Green means the site is open and the sky is forecast clear.
          </p>
        </section>

        <!-- Night selector: pick the nights you are free. Dead nights (no park open)
       are greyed and disabled. Filters the map, list and per-park scores. -->
        <div class="edition nights-card" class:collapsed={!nightsOpen}>
          <button
            type="button"
            class="nights-toggle"
            aria-expanded={nightsOpen}
            aria-controls="campsites-nights"
            onclick={() => (nightsOpen = !nightsOpen)}
          >
            <span class="nights-toggle-label">Nights you're free</span>
            <span class="nights-toggle-summary">{nightsSummary}</span>
            <span class="nights-toggle-chevron" aria-hidden="true"
              >{nightsOpen ? "▾" : "▸"}</span
            >
          </button>

          {#if nightsOpen}
            <div class="nights-body" id="campsites-nights">
              <div class="nights-actions">
                <Button
                  variant="secondary"
                  class="nights-act"
                  onclick={selectAllNights}>All</Button
                >
                <Button
                  variant="secondary"
                  class="nights-act"
                  onclick={selectWeekends}>Weekends</Button
                >
                <Button
                  variant="secondary"
                  class="nights-act"
                  onclick={clearNights}>Clear</Button
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
                  Pick the nights you are free, and matching parks will appear.
                </p>
              {/if}
            </div>
          {/if}
        </div>

        <!-- Ranked list stays in the control column; it never covers the map. -->
        <section class="edition list-panel" class:collapsed={!listOpen}>
          <header class="list-head">
            <div class="ed-head list-head-top">
              <p class="list-title">Ranked parks</p>
              <Button
                variant="secondary"
                class="collapse-btn"
                aria-expanded={listOpen}
                onclick={() => (listOpen = !listOpen)}
              >
                {listOpen ? "Hide" : "Show"}
              </Button>
            </div>

            {#if listOpen}
              <div class="controls">
                <div class="sort-row">
                  <span class="control-label">Sort</span>
                  <div class="toggle" role="group" aria-label="Sort parks by">
                    {#each [["best_score", "Sky score"], ["good_days", "Nights"], ["name", "Name"]] as [key, label] (key)}
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
                      aria-label="show only sites with at least one clear night"
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
        </section>
        <SiteIndex here="campsites" />
      </aside>
      <div class="content">
        <div class="edition map-stage">
          <CampsitesMap parks={visibleParks} bind:selectedId />
        </div>

        <!-- Forecast remains beside the controls and below the map in document flow. -->
        {#if selectedPark}
          <section
            class="edition detail"
            aria-label="{selectedPark.name} 14-day forecast"
          >
            <button
              type="button"
              class="detail-close"
              onclick={() => (selectedId = null)}
              aria-label="Close park detail">&times;</button
            >
            <div class="ed-head detail-head">
              <div class="detail-names">
                <h2 class="detail-park">{selectedPark.name}</h2>
                <p class="detail-region">
                  {selectedPark.region} &middot; {daysLabel(
                    selectedPark.good_days,
                  )}
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
                      <span class="day-temp"
                        >{Math.round(day.temp_max)}&deg;</span
                      >
                    {/if}
                    {#if day.precip != null}
                      <!-- Rain promoted out of the hover title so touch users (no
                     hover) can see it. Blue + mm on a wet day; a faint dot when
                     dry keeps the row rhythm without shouting "0.0". -->
                      <span
                        class="day-rain"
                        class:day-rain-wet={day.precip > 0}
                        style={day.precip > 0
                          ? `color: ${VIZ_RAIN}`
                          : undefined}
                        >{day.precip > 0
                          ? `${day.precip.toFixed(1)}mm`
                          : "·"}</span
                      >
                    {/if}
                  </li>
                {/each}
              </ul>
            </div>

            <p class="detail-legend">
              A check means the site is bookable. Green fades to grey as the
              forecast worsens, and closed sites are grey. The clear-sky score
              runs 0 to 100, higher is clearer. Each cell shows max temp (&deg;)
              and rain (mm, blue when wet).
            </p>
          </section>
        {/if}
      </div>
    </div>
  </div>
</main>
