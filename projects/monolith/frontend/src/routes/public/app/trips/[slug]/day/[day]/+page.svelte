<script>
  import DayNav from "$lib/public/components/trips/DayNav.svelte";
  import DayMap from "$lib/public/components/trips/DayMap.svelte";
  import DayStats from "$lib/public/components/trips/DayStats.svelte";
  import DayTelemetry from "$lib/public/components/trips/DayTelemetry.svelte";
  import ElevationChart from "$lib/public/components/trips/ElevationChart.svelte";
  import {
    groupByDay,
    dayColor,
    dayLabel,
    dayPhotos as photosOf,
    elevationSeries,
    clampIndex,
  } from "$lib/trips/trip.js";
  import { photoTelemetry } from "$lib/trips/telemetry.js";

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

    <div class="scrubber">
      <section class="photo-box" style={`--day:${color}`}>
        {#if photo}
          <figure class="frame">
            <img
              src={photo.imgDisplay}
              alt={`Photo ${current + 1} of ${photos.length}`}
              decoding="async"
            />
          </figure>
          <div class="controls">
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
        {:else}
          <p class="empty">No photos for this day.</p>
        {/if}
      </section>

      <section class="map-box" aria-label="Day route map">
        <DayMap
          points={day.points}
          {photos}
          dayColor={color}
          {current}
          {currentCoords}
          onPhotoClick={(i) => (current = i)}
        />
      </section>

      <section class="stats-box">
        <p class="eyebrow">Day stats</p>
        <DayStats stats={dayStats} dayColor={color} />
      </section>
    </div>

    {#if telemetry}
      <section class="telem-box">
        <p class="eyebrow">Telemetry</p>
        <DayTelemetry t={telemetry} index={current} total={photos.length} dayColor={color} />
      </section>
    {/if}

    {#if dayStats.hasElevation}
      <section class="profile">
        <p class="eyebrow">Elevation profile</p>
        <div class="chart">
          <ElevationChart
            {series}
            height={90}
            {color}
            cursor={cursorFraction}
            cursorColor={color}
            showMinMax={true}
          />
        </div>
      </section>
    {/if}
  {/if}
</div>

<style>
  .page {
    max-width: 1280px;
    margin: 0 auto;
    padding: 24px 24px 64px;
    background: var(--cream);
    color: var(--ink);
    min-height: 100vh;
  }
  /* Photo dominates: a wide left column (2fr) holds the large image; the map and
     day stats stack in the narrower right rail (1fr). */
  .scrubber {
    display: grid;
    grid-template-columns: 2fr 1fr;
    grid-template-areas:
      "photo map"
      "photo stats";
    gap: 20px;
    align-items: start;
  }
  .photo-box {
    grid-area: photo;
  }
  .map-box {
    grid-area: map;
    border: 2px solid var(--ink);
    height: 320px;
  }
  .stats-box {
    grid-area: stats;
  }
  .frame {
    margin: 0;
    border: 2px solid var(--ink);
    background: var(--ink);
    display: block;
  }
  .frame img {
    width: 100%;
    aspect-ratio: 16 / 9;
    max-height: 620px;
    object-fit: contain;
    display: block;
    background: var(--ink);
  }
  .controls {
    display: flex;
    align-items: stretch;
    margin-top: 12px;
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
    border: 2px solid var(--ink);
    border-left: none;
    border-right: none;
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
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 10px 14px;
    cursor: pointer;
  }
  .step:hover:not(:disabled) {
    background: var(--ink);
    color: var(--paper);
  }
  .step:disabled {
    opacity: 0.35;
    cursor: default;
  }
  .telem-box {
    margin-top: 28px;
  }
  .profile {
    margin-top: 28px;
  }
  .chart {
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 12px 14px;
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin: 0 0 12px;
  }
  .empty {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
  }
  .missing {
    font-family: var(--mono);
    color: var(--ink-3);
  }
  .missing a {
    color: var(--ink);
  }
  /* Narrow screens: stack photo, map, stats (in that order), then telemetry and
     elevation below (both already full width). */
  @media (max-width: 900px) {
    .scrubber {
      grid-template-columns: 1fr;
      grid-template-areas:
        "photo"
        "map"
        "stats";
    }
    .map-box {
      height: 280px;
    }
    .frame img {
      max-height: 70vh;
    }
  }
</style>
