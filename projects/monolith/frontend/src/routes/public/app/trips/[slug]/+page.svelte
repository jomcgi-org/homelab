<script>
  import { Button } from "@homelab/design-system/components";
  import TripSheet from "$lib/public/components/trips/TripSheet.svelte";
  import TripMap from "$lib/public/components/trips/TripMap.svelte";
  import ElevationChart from "$lib/public/components/trips/ElevationChart.svelte";
  import {
    deriveTripStats,
    dayColor,
    dayLabel,
    elevationSeries,
  } from "$lib/trips/trip.js";

  let { data } = $props();

  const trip = $derived(data.trip);
  const points = $derived(data.points ?? []);
  const stats = $derived(deriveTripStats(points, trip?.tz, trip?.stats));
  const highlights = $derived(trip?.highlights ?? []);

  let hoveredDay = $state(null);

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleDateString("en-US", {
        timeZone: trip?.tz || "UTC",
        month: "short",
        day: "numeric",
      });
    } catch {
      return "";
    }
  }

  function year(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleDateString("en-US", {
        timeZone: trip?.tz || "UTC",
        year: "numeric",
      });
    } catch {
      return "";
    }
  }

  function goToDay(n) {
    window.location.href = `/app/trips/${trip.slug}/day/${n}`;
  }

  // Client-side GPX / JSON export of the full route.
  function download(filename, text, type) {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function downloadGPX() {
    const trkpts = points
      .filter((p) => p.lat != null && p.lng != null)
      .map(
        (p) =>
          `      <trkpt lat="${p.lat}" lon="${p.lng}">${p.taken_at ? `<time>${p.taken_at}</time>` : ""}</trkpt>`,
      )
      .join("\n");
    const gpx = `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="jomcgi.dev" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>${trip.slug}</name></metadata>
  <trk><name>${trip.title}</name><trkseg>
${trkpts}
  </trkseg></trk>
</gpx>`;
    download(`${trip.slug}.gpx`, gpx, "application/gpx+xml");
  }

  function downloadJSON() {
    const payload = {
      slug: trip.slug,
      title: trip.title,
      points: points.map((p) => ({
        lat: p.lat,
        lng: p.lng,
        taken_at: p.taken_at,
        image: p.image ?? undefined,
        tags: p.tags?.length ? p.tags : undefined,
      })),
    };
    download(
      `${trip.slug}.json`,
      JSON.stringify(payload, null, 2),
      "application/json",
    );
  }
</script>

<svelte:head>
  <title>{trip?.title ?? "Trip"}</title>
  {#if trip?.subtitle}
    <meta name="description" content={trip.subtitle} />
  {/if}
</svelte:head>

<TripSheet {trip}>
  <header class="head edition ed-lead">
    <div class="title-row">
      <div>
        {#if stats}
          <p class="dateline">
            {fmtDate(stats.startIso)} &ndash; {fmtDate(stats.endIso)}, {year(
              stats.endIso,
            )}
          </p>
        {/if}
        <h1>{trip?.title}</h1>
        {#if trip?.subtitle}<p class="sub">{trip.subtitle}</p>{/if}
      </div>
      <a class="timeline-link" href={`/app/trips/${trip.slug}/timeline`}
        >Timeline &rarr;</a
      >
    </div>
  </header>

  {#if !stats}
    <p class="empty">No points for this trip yet.</p>
  {:else}
    <div class="layout">
      <div class="left">
        <div class="map-box">
          <div class="map-inner">
            <TripMap
              days={stats.days}
              {hoveredDay}
              onHover={(i) => (hoveredDay = i)}
              onDayClick={goToDay}
            />
          </div>
          <div class="colorbar">
            {#each stats.days as day, i (day.dayNumber)}
              <button
                class="bar"
                style={`background:${dayColor(i)};opacity:${hoveredDay === null || hoveredDay === i ? 1 : 0.25}`}
                aria-label={`Day ${day.dayNumber}`}
                onmouseenter={() => (hoveredDay = i)}
                onmouseleave={() => (hoveredDay = null)}
                onclick={() => goToDay(day.dayNumber)}
              ></button>
            {/each}
          </div>
        </div>

        {#if highlights.length}
          <section class="highlights">
            <p class="eyebrow">Highlights</p>
            <div class="hl-grid">
              {#each highlights as h (h.id)}
                <button
                  class="hl"
                  style={`--c:${dayColor((h.day ?? 1) - 1)}`}
                  onclick={() => goToDay(h.day)}
                  onmouseenter={() => (hoveredDay = (h.day ?? 1) - 1)}
                  onmouseleave={() => (hoveredDay = null)}
                >
                  {#if h.imgGallery}
                    <img src={h.imgGallery} alt={h.title} loading="lazy" />
                  {:else}
                    <span class="hl-fill" aria-hidden="true"></span>
                  {/if}
                  <span class="hl-cap">
                    <span class="hl-day">Day {h.day}</span>
                    <span class="hl-title">{h.title}</span>
                  </span>
                </button>
              {/each}
            </div>
          </section>
        {/if}
      </div>

      <div class="right">
        <div class="totals">
          <div class="tcell">
            <span class="label">Total distance</span>
            <span class="big"
              >{stats.totalDistance.toLocaleString()}<span class="unit">km</span
              ></span
            >
          </div>
          <div class="tcell">
            <span class="label">Duration</span>
            <span class="big"
              >{stats.totalDays}<span class="unit">days</span></span
            >
          </div>
          {#if stats.maxLat != null}
            <div class="tcell">
              <span class="label">Furthest north</span>
              <span class="big"
                >{stats.maxLat.toFixed(2)}<span class="unit">&deg;N</span></span
              >
            </div>
          {/if}
          {#if stats.coldestTemp != null}
            <div class="tcell">
              <span class="label">Coldest temp</span>
              <span class="big cold"
                >{stats.coldestTemp}<span class="unit">&deg;C</span></span
              >
            </div>
          {/if}
        </div>

        <section class="breakdown edition">
          <p class="ed-head">Daily routes</p>
          <ul class="days">
            {#each stats.days as day, i (day.dayNumber)}
              <li>
                <button
                  class="dayrow"
                  class:active={hoveredDay === i}
                  class:noelev={!stats.hasElevation}
                  style={`--c:${dayColor(i)}`}
                  onmouseenter={() => (hoveredDay = i)}
                  onmouseleave={() => (hoveredDay = null)}
                  onclick={() => goToDay(day.dayNumber)}
                >
                  <span class="dlabel"
                    >{dayLabel(trip.days, day.dayNumber)}</span
                  >
                  {#if stats.hasElevation}
                    <span class="profile">
                      <ElevationChart
                        series={elevationSeries(day.points)}
                        height={22}
                        min={stats.minElevation}
                        max={stats.maxElevation}
                      />
                    </span>
                  {/if}
                  <span class="dkm"
                    >{day.distance}<span class="unit">km</span></span
                  >
                  {#if stats.hasElevation}
                    <span class="delev">
                      <span class="up">+{day.ascent}</span>/<span class="down"
                        >-{day.descent}</span
                      >
                    </span>
                  {/if}
                  <span class="chev" aria-hidden="true">&rarr;</span>
                </button>
              </li>
            {/each}
          </ul>
        </section>

        <footer class="dl">
          <Button variant="secondary" onclick={downloadGPX}>&darr; GPX</Button>
          <Button variant="secondary" onclick={downloadJSON}>&darr; JSON</Button
          >
        </footer>
      </div>
    </div>
  {/if}
</TripSheet>

<style>
  .head {
    margin-bottom: 1.5rem;
  }
  .title-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .dateline {
    font: 0.75rem var(--font-code);
    color: var(--ink-2);
    margin: 0 0 0.5rem;
  }
  .timeline-link {
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--stroke);
    font: 0.8rem var(--font-code);
    text-decoration: none;
  }
  .timeline-link:hover {
    color: var(--accent-ink);
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 1.5rem;
  }
  .left,
  .right {
    min-width: 0;
  }
  .map-box {
    border: 1px solid var(--ink);
    margin-bottom: 1.5rem;
  }
  .map-inner {
    height: 280px;
  }
  .colorbar {
    display: flex;
    border-top: 1px solid var(--stroke);
  }
  .bar {
    flex: 1;
    min-width: 0;
    min-height: 24px;
    border: none;
    cursor: pointer;
    padding: 0;
  }
  .eyebrow {
    font: 0.8rem var(--font-code);
    color: var(--ink-2);
  }
  .hl-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border: 1px solid var(--ink);
    gap: 1px;
    background: var(--stroke);
  }
  .hl {
    display: flex;
    flex-direction: column;
    min-width: 0;
    padding: 0;
    border: none;
    background: var(--sheet);
    cursor: pointer;
    text-align: left;
  }
  .hl img,
  .hl-fill {
    width: 100%;
    aspect-ratio: 3 / 2;
    object-fit: cover;
    display: block;
  }
  .hl-fill {
    background: var(--band);
    border-bottom: 1px solid var(--c);
  }
  .hl-cap {
    padding: 0.6rem;
    border-top: 1px solid var(--stroke);
  }
  .hl-day,
  .hl-title {
    display: block;
    font: 0.75rem var(--font-code);
    color: var(--ink);
  }
  .hl-day {
    color: var(--ink-2);
  }
  .totals {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
    border: 1px solid var(--ink);
    margin-bottom: 1.5rem;
  }
  .tcell {
    padding: 1rem;
    border-right: 1px solid var(--stroke);
  }
  .label {
    display: block;
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
    margin-bottom: 0.5rem;
  }
  .big {
    font: 700 1.6rem var(--font-code);
    color: var(--ink);
  }
  .big.cold {
    color: var(--tone-gpu);
  }
  .unit {
    font-size: 0.7rem;
    color: var(--ink-2);
    margin-left: 3px;
  }
  .days {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .dayrow {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(0, 2fr) 4rem 6rem 1rem;
    align-items: center;
    gap: 1rem;
    padding: 0.8rem;
    border: none;
    border-bottom: 1px solid var(--stroke);
    background: var(--sheet);
    cursor: pointer;
    text-align: left;
  }
  .dayrow.noelev {
    grid-template-columns: minmax(0, 1fr) 5rem 1rem;
  }
  .dayrow.active {
    background: var(--band);
  }
  .dlabel {
    font: 0.8rem var(--font-code);
    border-bottom: 1px solid var(--c);
    padding-bottom: 4px;
    justify-self: start;
  }
  .profile {
    display: block;
  }
  .dkm,
  .delev {
    font: 0.8rem var(--font-code);
    text-align: right;
  }
  .up {
    color: var(--tone-ram);
  }
  .down {
    color: var(--tone-disk);
  }
  .chev {
    color: var(--ink-2);
    justify-self: end;
  }
  .dl {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 1.5rem;
    font: 0.8rem var(--font-code);
  }
  .empty {
    font-family: var(--font-code);
    color: var(--ink-2);
  }
  @media (max-width: 1000px) {
    .dayrow {
      grid-template-columns: minmax(0, 1fr) 4rem 1rem;
    }
    .profile,
    .delev {
      display: none;
    }
  }
</style>
