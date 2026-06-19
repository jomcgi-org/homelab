<script>
  import DayNav from "$lib/public/components/trips/DayNav.svelte";
  import DayMap from "$lib/public/components/trips/DayMap.svelte";
  import DayStats from "$lib/public/components/trips/DayStats.svelte";
  import PhotoGrid from "$lib/public/components/trips/PhotoGrid.svelte";
  import ElevationChart from "$lib/public/components/trips/ElevationChart.svelte";
  import {
    groupByDay,
    dayColor,
    dayLabel,
    dayPhotos as photosOf,
    elevationSeries,
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

    <div class="map-box">
      <DayMap points={day.points} {photos} dayColor={color} />
    </div>

    <DayStats stats={dayStats} dayColor={color} />

    {#if dayStats.hasElevation}
      <section class="profile">
        <p class="eyebrow">Elevation profile</p>
        <div class="chart">
          <ElevationChart
            series={elevationSeries(day.points, 120)}
            height={90}
            {color}
          />
        </div>
      </section>
    {/if}

    <section class="photos">
      <p class="eyebrow">{photos.length} photo{photos.length === 1 ? "" : "s"}</p>
      <PhotoGrid {photos} tz={trip?.tz} />
    </section>
  {/if}
</div>

<style>
  .page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 24px 24px 64px;
    background: var(--cream);
    color: var(--ink);
    min-height: 100vh;
  }
  .map-box {
    border: 2px solid var(--ink);
    height: 280px;
    margin-bottom: 24px;
  }
  .profile {
    margin-top: 28px;
  }
  .chart {
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 12px 14px;
  }
  .photos {
    margin-top: 28px;
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
  .missing {
    font-family: var(--mono);
    color: var(--ink-3);
  }
  .missing a {
    color: var(--ink);
  }
</style>
