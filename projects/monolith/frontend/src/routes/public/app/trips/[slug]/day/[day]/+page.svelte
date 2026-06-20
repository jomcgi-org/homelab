<script>
  import DayNav from "$lib/public/components/trips/DayNav.svelte";
  import DayMap from "$lib/public/components/trips/DayMap.svelte";
  import DayStats from "$lib/public/components/trips/DayStats.svelte";
  import ElevationChart from "$lib/public/components/trips/ElevationChart.svelte";
  import {
    groupByDay,
    dayColor,
    dayLabel,
    dayPhotos as photosOf,
    elevationSeries,
    clampIndex,
  } from "$lib/trips/trip.js";

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

  // Scrubber state: the index of the "current" photo. The map highlights and
  // centers this photo, the elevation cursor tracks its route position, and the
  // right-hand panel shows it at display size. Clamped to the photo count.
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

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString("en-CA", {
        timeZone: trip?.tz || "UTC",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return "";
    }
  }

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
      <section class="map-box" aria-label="Day route map">
        <DayMap
          points={day.points}
          {photos}
          dayColor={color}
          {current}
          onPhotoClick={(i) => (current = i)}
        />
      </section>

      <section class="stats-box">
        <p class="eyebrow">Day stats</p>
        <DayStats stats={dayStats} dayColor={color} />
      </section>

      <section class="photo-box" style={`--day:${color}`}>
        <p class="eyebrow">
          Photo {photos.length ? current + 1 : 0} / {photos.length}
        </p>
        {#if photo}
          <figure class="frame">
            <img
              src={photo.imgDisplay}
              alt={`Photo ${current + 1} of ${photos.length}`}
              decoding="async"
            />
            <figcaption>
              {#if photo.taken_at}<span>{fmtTime(photo.taken_at)}</span>{/if}
              {#if photo.focal_length_35mm}<span>{photo.focal_length_35mm}mm</span>{/if}
              {#if photo.aperture}<span>&fnof;/{photo.aperture}</span>{/if}
              {#if photo.iso}<span>ISO {photo.iso}</span>{/if}
              {#if photo.shutter_speed}<span>{photo.shutter_speed}</span>{/if}
              {#if photo.elevation != null}<span>{Math.round(photo.elevation)}m</span>{/if}
            </figcaption>
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
    </div>

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
  .scrubber {
    display: grid;
    grid-template-columns: 1.1fr 0.9fr 1.3fr;
    grid-template-areas: "map stats photo";
    gap: 20px;
    align-items: start;
  }
  .map-box {
    grid-area: map;
    border: 2px solid var(--ink);
    height: 420px;
  }
  .stats-box {
    grid-area: stats;
  }
  .photo-box {
    grid-area: photo;
  }
  .frame {
    margin: 0;
    border: 2px solid var(--ink);
    background: var(--ink);
    display: flex;
    flex-direction: column;
  }
  .frame img {
    width: 100%;
    max-height: 460px;
    object-fit: contain;
    display: block;
    background: var(--ink);
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    padding: 10px 12px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    color: var(--paper);
    border-top: 2px solid var(--day);
  }
  .controls {
    display: flex;
    gap: 0;
    margin-top: 12px;
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
  .step + .step {
    border-left: none;
  }
  .step:hover:not(:disabled) {
    background: var(--ink);
    color: var(--paper);
  }
  .step:disabled {
    opacity: 0.35;
    cursor: default;
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
  /* Narrow screens: stack map, photo, stats (in that order), then elevation. */
  @media (max-width: 900px) {
    .scrubber {
      grid-template-columns: 1fr;
      grid-template-areas:
        "map"
        "photo"
        "stats";
    }
    .map-box {
      height: 300px;
    }
  }
</style>
