<script>
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

<div class="page">
  <header class="head">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="https://jomcgi.dev/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
        ></a
      >
      <span class="crumb-sep">/</span>
      <a class="crumb-link" href="/app/trips">trips</a>
      <span class="crumb-sep">/</span>
      <span class="crumb-name">{trip?.short_title ?? trip?.slug}</span>
    </nav>

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
      <a class="btn" href={`/app/trips/${trip.slug}/timeline`}
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

        <section class="breakdown">
          <p class="eyebrow">Daily routes</p>
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
          <button class="btn small" onclick={downloadGPX}>&darr; GPX</button>
          <button class="btn small" onclick={downloadJSON}>&darr; JSON</button>
        </footer>
      </div>
    </div>
  {/if}
</div>

<style>
  .page {
    max-width: 1280px;
    margin: 0 auto;
    padding: 28px 24px 64px;
    background: var(--cream);
    color: var(--ink);
    min-height: 100vh;
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
    margin-bottom: 18px;
  }
  .crumb-home,
  .crumb-link {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
    padding: 0 2px;
  }
  .crumb-home:hover,
  .crumb-link:hover {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
  }
  .crumb-sep {
    color: var(--ink-3);
  }
  .title-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 28px;
  }
  .dateline {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin: 0 0 6px;
  }
  h1 {
    font-family: var(--serif);
    font-size: 40px;
    line-height: 1;
    margin: 0;
  }
  .sub {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-2);
    margin: 6px 0 0;
  }
  .btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 10px 16px;
    border: 2px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    text-decoration: none;
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .btn:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }
  .btn.small {
    padding: 8px 14px;
  }
  .layout {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 40px;
    align-items: start;
  }
  .map-box {
    border: 2px solid var(--ink);
    margin-bottom: 20px;
  }
  .map-inner {
    height: 280px;
  }
  .colorbar {
    display: flex;
    border-top: 2px solid var(--ink);
  }
  .bar {
    flex: 1;
    height: 8px;
    border: none;
    cursor: pointer;
    padding: 0;
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
  .hl-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 2px;
    border: 2px solid var(--ink);
  }
  .hl {
    position: relative;
    aspect-ratio: 1;
    padding: 0;
    border: none;
    background: var(--ink);
    cursor: pointer;
    overflow: hidden;
  }
  .hl img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .hl-fill {
    display: block;
    width: 100%;
    height: 100%;
    background: var(--c);
  }
  .hl-cap {
    position: absolute;
    inset: auto 0 0 0;
    padding: 8px 10px;
    background: linear-gradient(transparent, rgba(0, 0, 0, 0.82));
    text-align: left;
  }
  .hl-day {
    display: block;
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--paper);
  }
  .hl-title {
    display: block;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--paper);
  }
  .totals {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    border: 2px solid var(--ink);
    border-bottom: none;
    margin-bottom: 32px;
  }
  .tcell {
    padding: 16px 18px;
    border-bottom: 2px solid var(--ink);
    border-right: 2px solid var(--ink);
    background: var(--paper);
  }
  .tcell:last-child {
    border-right: none;
  }
  .label {
    display: block;
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 8px;
  }
  .big {
    font-family: var(--mono);
    font-size: 34px;
    font-weight: 900;
    line-height: 1;
    color: var(--ink);
  }
  .big.cold {
    color: var(--blue);
  }
  .unit {
    font-size: 14px;
    font-weight: 700;
    color: var(--ink-3);
    margin-left: 3px;
  }
  .days {
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: 2px solid var(--ink);
  }
  .dayrow {
    width: 100%;
    display: grid;
    grid-template-columns: 1.4fr 2fr 70px 90px 18px;
    align-items: center;
    gap: 16px;
    padding: 12px 10px;
    border-bottom: 2px solid var(--ink);
    background: transparent;
    cursor: pointer;
    text-align: left;
  }
  .dayrow.noelev {
    grid-template-columns: 1fr 80px 18px;
  }
  .dayrow.active {
    background: var(--ink);
  }
  .dayrow.active .dlabel,
  .dayrow.active .dkm {
    color: var(--paper);
  }
  .dlabel {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--ink);
    border-bottom: 2px solid var(--c);
    padding-bottom: 4px;
    justify-self: start;
  }
  .profile {
    display: block;
  }
  .dayrow.active .profile {
    filter: invert(1);
  }
  .dkm {
    font-family: var(--mono);
    font-size: 14px;
    font-weight: 900;
    text-align: right;
    color: var(--ink);
  }
  .delev {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    text-align: right;
  }
  .up {
    color: var(--teal);
  }
  .down {
    color: var(--coral);
  }
  .chev {
    font-family: var(--mono);
    color: var(--ink-3);
    justify-self: end;
  }
  .dayrow.active .chev {
    color: var(--paper);
  }
  .dl {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 28px;
    padding-top: 18px;
    border-top: 2px solid var(--ink);
  }
  .empty {
    font-family: var(--mono);
    color: var(--ink-3);
  }

  @media (max-width: 860px) {
    .layout {
      grid-template-columns: 1fr;
      gap: 28px;
    }
    h1 {
      font-size: 30px;
    }
    .dayrow {
      grid-template-columns: 1fr 60px 18px;
    }
    .profile,
    .delev {
      display: none;
    }
  }
</style>
