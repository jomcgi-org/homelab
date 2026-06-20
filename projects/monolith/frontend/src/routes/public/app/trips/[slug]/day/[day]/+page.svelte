<script>
  import DayNav from "$lib/public/components/trips/DayNav.svelte";
  import DayMap from "$lib/public/components/trips/DayMap.svelte";
  import ElevationChart from "$lib/public/components/trips/ElevationChart.svelte";
  import {
    groupByDay,
    dayColor,
    dayLabel,
    dayPhotos as photosOf,
    elevationSeries,
    clampIndex,
  } from "$lib/trips/trip.js";
  import { photoTelemetry, formatCoord } from "$lib/trips/telemetry.js";

  let { data } = $props();

  const trip = $derived(data.trip);
  const dayNumber = $derived(data.dayNumber);
  const days = $derived(groupByDay(data.points ?? [], trip?.tz));
  const totalDays = $derived(days.length);
  const day = $derived(days.find((d) => d.dayNumber === dayNumber) ?? null);
  const color = $derived(dayColor((dayNumber || 1) - 1));
  const photos = $derived(day ? photosOf(day.points) : []);
  const label = $derived(dayLabel(trip?.days, dayNumber));
  const dayStats = $derived(day ? { ...day, photoCount: photos.length } : null);
  const series = $derived(day ? elevationSeries(day.points, 120) : []);

  // Scrubber state: the index of the "current" photo. The photo is the dominant
  // element; the map highlights and centers it, the elevation cursor tracks its
  // route position, and the telemetry panel shows its per-photo readouts.
  let current = $state(0);

  // Reset to the first photo whenever the day changes (SvelteKit reuses this
  // component across [day] navigations, so $state(0) alone would persist a stale
  // index). Also keeps `current` in range if the photo list shrinks.
  $effect(() => {
    const _ = dayNumber;
    current = 0;
  });
  $effect(() => {
    if (current > photos.length - 1) current = clampIndex(current, photos.length);
  });

  const photo = $derived(photos[current] ?? null);

  // Per-photo telemetry (position/elev interpolated from the GPS track when the
  // photo lacks a fix, plus bearing, cumulative km, EV and solar context).
  const telemetry = $derived(
    photo ? photoTelemetry(photo, day?.points ?? [], trip?.tz) : null,
  );

  // Interpolated coordinates for the map's current-photo marker, so it still
  // tracks photos that lack their own GPS fix.
  const currentCoords = $derived(
    telemetry && telemetry.lat != null && telemetry.lng != null
      ? [telemetry.lng, telemetry.lat]
      : null,
  );

  function step(delta) {
    if (!photos.length) return;
    current = clampIndex(current + delta, photos.length);
  }

  // Cursor fraction (0..1) along the elevation x-axis. The elevation chart is
  // sampled over every point in the day; map the current photo to its position
  // in that point sequence so the cursor tracks where the photo sits along the
  // route. Approximate (photo points are a subset of all points) but tracking.
  const cursorFraction = $derived.by(() => {
    if (!photo || !day || day.points.length < 2) return null;
    const idx = day.points.indexOf(photo);
    if (idx < 0) return null;
    return idx / (day.points.length - 1);
  });

  // Warm the browser/CDN cache for the neighbouring photos so arrow-stepping
  // shows the next image instantly. Pre-signed `imgDisplay` URLs are already in
  // the SSR payload, so this just kicks off background GETs via new Image(); it
  // never blocks render. Browser-only (Image is undefined during SSR).
  $effect(() => {
    const i = current;
    if (typeof Image === "undefined" || !photos.length) return;
    const preloaders = [];
    for (const delta of [1, -1, 2, -2]) {
      const neighbor = photos[i + delta];
      if (neighbor?.imgDisplay) {
        const img = new Image();
        img.src = neighbor.imgDisplay;
        preloaders.push(img);
      }
    }
    // Drop references on change/teardown so the GC can reclaim them once the
    // browser cache is warm (no listeners are attached, so nothing leaks).
    return () => {
      preloaders.length = 0;
    };
  });

  // Arrow keys step through photos. No scroll-jacking: keyboard + buttons only.
  $effect(() => {
    const onKey = (e) => {
      if (!photos.length) return;
      if (e.key === "ArrowLeft") {
        step(-1);
      } else if (e.key === "ArrowRight") {
        step(1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
</script>

<svelte:head>
  <title>{trip ? `${trip.short_title ?? trip.title} - Day ${dayNumber}` : "Day"}</title>
</svelte:head>

<div class="page">
  {#if !day}
    <p class="missing">
      Day {dayNumber} not found. <a href={`/app/trips/${trip?.slug}`}>Back to summary</a>.
    </p>
  {:else}
    <DayNav slug={trip.slug} {dayNumber} {totalDays} {label} dayColor={color} />

    <!-- One bordered instrument panel: photo (dominant), map + telemetry rail,
         controls, elevation strip and day totals all share contiguous 2px ink
         gaps so the dashboard reads as a single connected unit that fits one
         viewport. The ink background + 2px grid gap renders every divider; each
         cell paints its own paper/cream fill on top. -->
    <div class="panel" style={`--day:${color}`}>
      <!-- PHOTO: the dominant cell, letterboxed (contain) so it never forces the
           page taller than the viewport. -->
      <figure class="photo">
        {#if photo}
          <img
            src={photo.imgDisplay}
            alt={`Photo ${current + 1} of ${photos.length}`}
            decoding="async"
          />
        {:else}
          <span class="empty">No photos for this day.</span>
        {/if}
      </figure>

      <!-- CONTROLS: prev / counter / next, locked under the photo. -->
      <div class="ctrls">
        <button
          class="step"
          onclick={() => step(-1)}
          disabled={current === 0}
          aria-label="Previous photo"
        >
          &larr; Prev
        </button>
        <span class="counter">{photos.length ? current + 1 : 0} / {photos.length}</span>
        <button
          class="step"
          onclick={() => step(1)}
          disabled={current >= photos.length - 1}
          aria-label="Next photo"
        >
          Next &rarr;
        </button>
      </div>

      <!-- MAP: top of the right rail. -->
      <div class="map" aria-label="Day route map">
        <DayMap
          points={day.points}
          {photos}
          dayColor={color}
          {current}
          {currentCoords}
          onPhotoClick={(i) => (current = i)}
        />
      </div>

      <!-- TELEMETRY: the per-photo machine readout, beside the photo (mirrors the
           original DataPanel). All fields preserved. -->
      <div class="telem">
        {#if telemetry}
          {@const t = telemetry}
          <div class="cell time">
            <span class="label">Time</span>
            <span class="clock">{t.time}<span class="period">{t.period}</span></span>
          </div>
          <div class="cell">
            <span class="label">Solar</span>
            <span class="value">{t.solarAltDeg != null ? `${Math.round(t.solarAltDeg)}°` : "--"}</span>
            <span class="sub">{t.solarLabel}</span>
          </div>
          <div class="cell">
            <span class="label">Light</span>
            <span class="value sm">{t.light || "DARK"}</span>
          </div>
          <div class="cell">
            <span class="label">EV</span>
            <span class="value">{t.ev ?? "--"}</span>
            <span class="sub">{t.evLabel}</span>
          </div>
          <div class="cell">
            <span class="label">Elev</span>
            <span class="value">{t.elevation != null ? Math.round(t.elevation) : "--"}<span class="unit">m</span></span>
          </div>
          <div class="cell">
            <span class="label">Km</span>
            <span class="value">{t.km ?? 0}<span class="unit">/{t.totalKm ?? 0}</span></span>
          </div>
          <div class="cell bearing">
            <span class="label">Bearing</span>
            <span class="arrow">{t.bearingArrow}</span>
            <span class="sub">{t.bearing != null ? `${Math.round(t.bearing)}°` : "--"}</span>
          </div>
          <div class="cell">
            <span class="label">Photo</span>
            <span class="value">{photos.length ? current + 1 : 0}<span class="unit">/{photos.length}</span></span>
          </div>
          <div class="cell optics">
            <span class="label">Optics</span>
            <span class="value sm">
              {t.focalLength35mm != null ? `${t.focalLength35mm}mm` : "--"} &fnof;/{t.aperture ?? "--"}
            </span>
            <span class="sub">ISO {t.iso ?? "--"} · {t.shutterSpeed ?? "--"}</span>
          </div>
          <div class="cell position">
            <span class="label">Position</span>
            <span class="coord">{formatCoord(t.lat, true)}</span>
            <span class="coord">{formatCoord(t.lng, false)}</span>
          </div>
        {:else}
          <div class="cell"><span class="label">Telemetry</span><span class="value sm">--</span></div>
        {/if}
      </div>

      <!-- ELEVATION: connected strip across the foot of the panel. -->
      <div class="elev">
        <span class="label">Elevation profile</span>
        {#if dayStats.hasElevation}
          <div class="chart">
            <ElevationChart
              {series}
              height={88}
              {color}
              cursor={cursorFraction}
              cursorColor={color}
              showMinMax={true}
            />
          </div>
        {:else}
          <span class="value sm none">No elevation data</span>
        {/if}
      </div>

      <!-- DAY TOTALS: subsumed into the panel, no separate floating card. -->
      <div class="stats">
        <div class="cell">
          <span class="label">Distance</span>
          <span class="value">{dayStats.distance}<span class="unit">km</span></span>
        </div>
        {#if dayStats.hasElevation}
          <div class="cell">
            <span class="label">Ascent</span>
            <span class="value up">+{dayStats.ascent.toLocaleString()}<span class="unit">m</span></span>
          </div>
          <div class="cell">
            <span class="label">Descent</span>
            <span class="value down">-{dayStats.descent.toLocaleString()}<span class="unit">m</span></span>
          </div>
          {#if dayStats.maxElevation != null}
            <div class="cell">
              <span class="label">High</span>
              <span class="value">{dayStats.maxElevation.toLocaleString()}<span class="unit">m</span></span>
            </div>
          {/if}
          {#if dayStats.minElevation != null}
            <div class="cell">
              <span class="label">Low</span>
              <span class="value">{dayStats.minElevation.toLocaleString()}<span class="unit">m</span></span>
            </div>
          {/if}
        {/if}
        <div class="cell">
          <span class="label">Photos</span>
          <span class="value">{dayStats.photoCount ?? 0}</span>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .page {
    max-width: 1280px;
    margin: 0 auto;
    padding: 16px 24px 24px;
    background: var(--cream);
    color: var(--ink);
    min-height: 100vh;
  }

  /* The instrument panel. One grid, contiguous 2px ink dividers (ink background
     showing through the 2px gap), framed by a 2px border. Sized to fill the
     viewport below the day-nav so the whole day reads without scrolling on a
     normal desktop. Rows: a flexing top band (photo + map) then auto-height
     telemetry, controls, elevation and totals. */
  .panel {
    display: grid;
    grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
    grid-template-rows: minmax(150px, 1fr) auto auto auto;
    grid-template-areas:
      "photo map"
      "photo telem"
      "ctrls telem"
      "elev stats";
    gap: 2px;
    background: var(--ink);
    border: 2px solid var(--ink);
    /* Fill the viewport minus the page padding + day-nav header. */
    height: calc(100vh - 132px);
    min-height: 520px;
  }

  .photo {
    grid-area: photo;
    margin: 0;
    background: var(--ink);
    min-height: 0;
    min-width: 0;
    overflow: hidden;
    display: flex;
  }
  .photo img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
    background: var(--ink);
  }
  .photo .empty {
    margin: auto;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--paper);
  }

  .ctrls {
    grid-area: ctrls;
    display: flex;
    align-items: stretch;
    background: var(--paper);
    min-width: 0;
  }
  .counter {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    padding: 0 18px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--ink);
    background: var(--paper);
  }
  .step {
    flex: 1;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink);
    border: none;
    background: var(--paper);
    padding: 12px 14px;
    cursor: pointer;
  }
  .step:first-child {
    border-right: 2px solid var(--ink);
  }
  .step:last-child {
    border-left: 2px solid var(--ink);
  }
  .step:hover:not(:disabled) {
    background: var(--ink);
    color: var(--paper);
  }
  .step:disabled {
    opacity: 0.35;
    cursor: default;
  }

  .map {
    grid-area: map;
    background: var(--paper);
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }

  /* Telemetry: a nested grid that inherits the same ink-gap treatment, so its
     internal dividers line up visually with the rest of the panel. */
  .telem {
    grid-area: telem;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
    grid-auto-rows: minmax(0, 1fr);
    gap: 2px;
    background: var(--ink);
    font-family: var(--mono);
    min-width: 0;
    min-height: 0;
  }
  .cell {
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 10px 12px;
    background: var(--paper);
    min-width: 0;
    overflow: hidden;
  }
  .label {
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 5px;
  }
  .value {
    font-family: var(--mono);
    font-size: 18px;
    font-weight: 900;
    line-height: 1;
    color: var(--ink);
  }
  .value.sm {
    font-size: 12px;
    font-weight: 700;
  }
  .unit {
    font-size: 11px;
    font-weight: 700;
    color: var(--ink-3);
    margin-left: 2px;
  }
  .sub {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--ink-3);
    margin-top: 4px;
  }
  .coord {
    font-size: 11px;
    font-weight: 700;
    line-height: 1.5;
    color: var(--ink);
  }
  .up {
    color: var(--teal);
  }
  .down {
    color: var(--coral);
  }
  /* TIME: the primary readout, accent-topped, spanning the rail width. */
  .time {
    grid-column: 1 / -1;
    border-top: 3px solid var(--day);
  }
  .clock {
    font-size: 26px;
    font-weight: 900;
    line-height: 1;
    color: var(--ink);
  }
  .period {
    font-size: 12px;
    font-weight: 700;
    color: var(--ink-3);
    margin-left: 4px;
  }
  .bearing .arrow {
    font-size: 26px;
    line-height: 1;
    color: var(--ink);
  }
  .optics {
    grid-column: span 2;
  }
  .position {
    grid-column: span 2;
  }

  /* Elevation strip, foot of the panel under the photo. */
  .elev {
    grid-area: elev;
    display: flex;
    flex-direction: column;
    padding: 10px 14px;
    background: var(--paper);
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  .elev .chart {
    flex: 1;
    min-height: 0;
    display: flex;
    align-items: stretch;
  }
  .elev .chart :global(.elev) {
    width: 100%;
    align-self: center;
  }
  .elev .none {
    margin-top: 8px;
    color: var(--ink-3);
  }

  /* Day totals: nested ink-gap grid, contiguous with the panel. */
  .stats {
    grid-area: stats;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(72px, 1fr));
    gap: 2px;
    background: var(--ink);
    min-width: 0;
  }

  /* Narrow screens: drop the fit-to-viewport constraint and stack the panel into
     a single scrollable column (photo, controls, map, telemetry, elevation,
     totals). */
  @media (max-width: 900px) {
    .panel {
      grid-template-columns: 1fr;
      grid-template-rows: auto auto auto auto auto auto;
      grid-template-areas:
        "photo"
        "ctrls"
        "map"
        "telem"
        "elev"
        "stats";
      height: auto;
      min-height: 0;
    }
    .photo {
      aspect-ratio: 4 / 3;
      max-height: 70vh;
    }
    .map {
      height: 280px;
    }
    .elev .chart {
      min-height: 96px;
    }
  }
</style>
