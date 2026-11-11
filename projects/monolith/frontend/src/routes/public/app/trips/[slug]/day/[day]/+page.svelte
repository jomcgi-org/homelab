<script>
  import TripSheet from "$lib/public/components/trips/TripSheet.svelte";
  import DayNav from "$lib/public/components/trips/DayNav.svelte";
  import DayMap from "$lib/public/components/trips/DayMap.svelte";
  import DayElevationProfile from "$lib/public/components/trips/DayElevationProfile.svelte";
  import {
    groupByDay,
    dayColor,
    dayLabel,
    dayPhotos as photosOf,
    elevationSeries,
    clampIndex,
  } from "$lib/trips/trip.js";
  import { photoTelemetry, formatCoord } from "$lib/trips/telemetry.js";
  import { sunAltitude, sunAzimuth } from "$lib/trips/sun.js";

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

  // Day date for the nav eyebrow, formatted in the trip's zone (e.g. "Sep 15,
  // 2024"). Taken from the day's first point so it never disagrees with the
  // day-grouping key.
  const formattedDate = $derived.by(() => {
    const iso = day?.points?.[0]?.taken_at;
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    try {
      return d.toLocaleDateString("en-US", {
        timeZone: trip?.tz || "UTC",
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    } catch {
      return "";
    }
  });

  // Scrubber state: index of the "current" photo. The photo is the dominant
  // element; the map highlights/centres it, the elevation cursor tracks its
  // route position, and the data panel shows its per-photo readouts.
  let current = $state(0);
  let showFullscreen = $state(false);

  // Reset to the first photo whenever the day changes (SvelteKit reuses this
  // component across [day] navigations). Also keeps `current` in range if the
  // photo list shrinks.
  $effect(() => {
    const _ = dayNumber;
    current = 0;
    showFullscreen = false;
  });
  $effect(() => {
    if (current > photos.length - 1)
      current = clampIndex(current, photos.length);
  });

  const photo = $derived(photos[current] ?? null);

  // Smooth scrubbing: keep the CURRENT frame on screen until the next image has
  // actually decoded (instant when preloaded), so the photo never flashes its
  // empty cell between shots while an arrow is held. Only the latest target wins.
  let displayedSrc = $state(photos[current]?.imgDisplay ?? null);
  $effect(() => {
    const target = photo?.imgDisplay ?? null;
    if (!target) {
      displayedSrc = null;
      return;
    }
    if (typeof Image === "undefined") {
      displayedSrc = target; // SSR: render the URL directly
      return;
    }
    let cancelled = false;
    const swap = () => {
      if (!cancelled) displayedSrc = target;
    };
    const img = new Image();
    img.src = target;
    if (img.complete) swap();
    else if (img.decode) img.decode().then(swap).catch(swap);
    else img.onload = swap;
    return () => {
      cancelled = true;
    };
  });

  // Per-photo telemetry (position/elev interpolated from the GPS track when the
  // photo lacks a fix, plus bearing, cumulative km, EV and solar context).
  const telemetry = $derived(
    photo ? photoTelemetry(photo, day?.points ?? [], trip?.tz) : null,
  );

  // Interpolated [lng, lat] for the map's current-photo marker, so it tracks
  // photos that lack their own GPS fix.
  const currentCoords = $derived(
    telemetry && telemetry.lat != null && telemetry.lng != null
      ? [telemetry.lng, telemetry.lat]
      : null,
  );

  // Sun altitude + azimuth (radians) at the photo's capture time and location,
  // for the map's terrain hillshade relighting (mirrors the original's
  // SunCalc.getPosition()).
  const sunPosition = $derived.by(() => {
    if (!telemetry || telemetry.lat == null || telemetry.lng == null)
      return null;
    if (!photo?.taken_at) return null;
    const d = new Date(photo.taken_at);
    if (Number.isNaN(d.getTime())) return null;
    return {
      altitude: sunAltitude(d, telemetry.lat, telemetry.lng),
      azimuth: sunAzimuth(d, telemetry.lat, telemetry.lng),
    };
  });

  // Elevation sparkline: ~60 samples for the polyline, with the position marker
  // placed by the photo's continuous progress fraction along the route. Passing
  // the raw fraction (not a rounded sample index) lets the marker sit at its true
  // position and glide between photos instead of snapping to one of ~60 buckets.
  const profile = $derived(day ? elevationSeries(day.points, 60) : []);
  const profileProgress = $derived(telemetry?.progressFraction ?? 0);

  function step(delta) {
    if (!photos.length) return;
    current = clampIndex(current + delta, photos.length);
  }

  // Map route click -> nearest route point's capture time -> nearest photo.
  function handleMapLocationClick(takenAtIso) {
    if (!photos.length || !takenAtIso) return;
    const clickTime = new Date(takenAtIso).getTime();
    if (Number.isNaN(clickTime)) return;
    let closest = 0;
    let minDiff = Infinity;
    photos.forEach((p, i) => {
      if (!p.taken_at) return;
      const diff = Math.abs(new Date(p.taken_at).getTime() - clickTime);
      if (diff < minDiff) {
        minDiff = diff;
        closest = i;
      }
    });
    current = closest;
  }

  // Warm the browser/CDN cache so scrubbing is always instant: first the close
  // neighbours (+-5, nearest first), then stream the rest of the day forward in
  // the background (then backward as filler), paced so we never fire hundreds of
  // requests at once. Browser-only. `warmed` persists across scrubs.
  const warmed = new Set();
  function warm(idx) {
    const p = photos[idx];
    if (!p?.imgDisplay || warmed.has(p.imgDisplay)) return false;
    warmed.add(p.imgDisplay);
    new Image().src = p.imgDisplay;
    return true;
  }
  $effect(() => {
    const i = current;
    if (typeof Image === "undefined" || !photos.length) return;
    for (let d = 1; d <= 5; d++) {
      warm(i + d);
      warm(i - d);
    }
    let fwd = i + 6;
    let back = i - 6;
    const timer = setInterval(() => {
      let did = false;
      for (let n = 0; n < 3 && !did; n++) {
        while (fwd < photos.length && !(did = warm(fwd++)));
        if (!did) while (back >= 0 && !(did = warm(back--)));
      }
      if (fwd >= photos.length && back < 0) clearInterval(timer);
    }, 100);
    return () => clearInterval(timer);
  });

  // Arrow keys step through photos; Escape closes the fullscreen overlay.
  $effect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        showFullscreen = false;
        return;
      }
      if (!photos.length) return;
      if (e.key === "ArrowLeft") step(-1);
      else if (e.key === "ArrowRight") step(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const hasPrev = $derived(current > 0);
  const hasNext = $derived(current < photos.length - 1);
</script>

<svelte:head>
  <title
    >{trip
      ? `${trip.short_title ?? trip.title} - Day ${dayNumber}`
      : "Day"}</title
  >
</svelte:head>

<TripSheet {trip} current={label}>
  <div class="day-content">
    {#if !day}
      <p class="missing">
        Day {dayNumber} not found.
        <a href={`/app/trips/${trip?.slug}`}>Back to summary</a>.
      </p>
    {:else}
      <DayNav
        slug={trip.slug}
        {dayNumber}
        {totalDays}
        {label}
        dayColor={color}
        date={formattedDate}
      />

      <div class="stack">
        <!-- The map keeps its route selection and photo-time hillshade. -->
        <DayMap
          points={day.points}
          dayColor={color}
          height="280px"
          {currentCoords}
          {sunPosition}
          onLocationClick={handleMapLocationClick}
        />

        <!-- Photo and per-photo telemetry share one ruled panel. -->
        <div class="triptych">
          {#if photo}
            <button
              class="photo"
              onclick={() => (showFullscreen = true)}
              aria-label={`View photo ${current + 1} fullscreen`}
            >
              <img
                src={displayedSrc}
                alt={`Photo ${current + 1} of ${photos.length}`}
                decoding="async"
              />
            </button>
          {:else}
            <div class="photo empty">No photos for this day.</div>
          {/if}

          <div class="panel">
            {#if telemetry}
              {@const t = telemetry}
              <!-- ROW 1: TIME | SOLAR | LIGHT | EV | ELEV -->
              <!-- Day colour marks the time cell without changing text contrast. -->
              <div
                class="cell r1 c-time"
                style={`border-bottom:1px solid ${color}`}
              >
                <div class="label">TIME</div>
                <div class="time-big">
                  {t.time}<span class="period">{t.period}</span>
                </div>
              </div>
              <div class="cell r1 div-thin">
                <div class="label">SOLAR</div>
                <div class="val">
                  {t.solarAltDeg != null
                    ? `${t.solarAltDeg.toFixed(0)}°`
                    : "--"}
                </div>
                <div class="sub">{t.solarLabel}</div>
              </div>
              <div class="cell r1 div-thin">
                <div class="label">LIGHT</div>
                <div class="val sm">{t.light || "DARK"}</div>
              </div>
              <div class="cell r1 div-thin">
                <div class="label">EV</div>
                <div class="val">{t.ev ?? "--"}</div>
                <div class="sub">{t.evLabel}</div>
              </div>
              <div class="cell r1">
                <div class="label">ELEV</div>
                <div class="val">
                  {t.elevation != null ? Math.round(t.elevation) : "--"}<span
                    class="unit">m</span
                  >
                </div>
              </div>

              <!-- ROW 2: OPTICS+NAV+BEARING | ELEVATION PROFILE -->
              <div class="col1">
                <div class="optics">
                  <div class="label">OPTICS</div>
                  <div class="optics-line">
                    {t.focalLength35mm != null
                      ? `${t.focalLength35mm}mm`
                      : "--"} &fnof;/{t.aperture ?? "--"}
                  </div>
                  <div class="optics-sub">
                    ISO {t.iso ?? "--"} · {t.shutterSpeed ?? "--"}
                  </div>
                </div>

                <div class="nav">
                  <div class="navrow">
                    <button
                      class="navstep border-r"
                      onclick={() => step(-1)}
                      disabled={!hasPrev}
                      aria-label="Previous photo"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                        ><polyline points="15 18 9 12 15 6" /></svg
                      >
                    </button>
                    <button
                      class="navstep"
                      onclick={() => step(1)}
                      disabled={!hasNext}
                      aria-label="Next photo"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                        ><polyline points="9 18 15 12 9 6" /></svg
                      >
                    </button>
                  </div>
                  <div
                    class="counter"
                    style={`border-bottom:1px solid ${color}`}
                  >
                    {photos.length ? current + 1 : 0} / {photos.length}
                  </div>
                </div>

                <div class="bearing">
                  <div class="label">BEARING</div>
                  <div class="arrow">{t.bearingArrow}</div>
                  <div class="bearing-deg">
                    {t.bearing != null ? `${Math.round(t.bearing)}°` : "--"}
                  </div>
                </div>
              </div>

              <div class="elev-profile">
                <div class="label">ELEVATION PROFILE</div>
                <div class="elev-inner">
                  <DayElevationProfile
                    data={profile}
                    progress={profileProgress}
                    accentColor={color}
                  />
                </div>
              </div>

              <!-- ROW 3: POSITION | KM | ASCENT | DESCENT | PHOTOS -->
              <div class="cell-position">
                <div class="label">POSITION</div>
                <div class="coords">
                  <div>{formatCoord(t.lat, true)}</div>
                  <div>{formatCoord(t.lng, false)}</div>
                </div>
              </div>
              <div class="cell-stat div-thin">
                <div class="label">KM</div>
                <div class="km-val">
                  {t.km ?? 0}<span class="small">/{t.totalKm ?? 0}</span>
                </div>
              </div>
              <div class="cell-stat div-thin">
                <div class="label">ASCENT</div>
                <div class="stat-val up">
                  +{(dayStats?.ascent ?? 0).toLocaleString()}m
                </div>
              </div>
              <div class="cell-stat div-thin">
                <div class="label">DESCENT</div>
                <div class="stat-val down">
                  -{(dayStats?.descent ?? 0).toLocaleString()}m
                </div>
              </div>
              <div class="cell-stat">
                <div class="label">PHOTOS</div>
                <div class="stat-val">{dayStats?.photoCount ?? 0}</div>
              </div>
            {:else}
              <div class="cell r1">
                <div class="label">TELEMETRY</div>
                <div class="val sm">no data for this photo</div>
              </div>
            {/if}
          </div>
        </div>

        {#if Array.isArray(trip.highlights) && trip.highlights.length > 0}
          <!-- (Trip-level highlights; the original showed per-day highlights here.) -->
        {/if}
      </div>
    {/if}
  </div>

  {#if showFullscreen && photo}
    <button
      class="fs"
      aria-label="Close fullscreen photo"
      onclick={() => (showFullscreen = false)}
    >
      <img
        src={photo.imgGallery ?? displayedSrc}
        alt={`Photo ${current + 1} of ${photos.length}`}
      />
    </button>
  {/if}
</TripSheet>

<style>
  .missing {
    font-family: var(--font-code);
    font-size: 13px;
  }
  .stack {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  /* Photo and data share the sheet outline. */
  .triptych {
    display: flex;
    align-items: stretch;
    border: 1px solid var(--ink);
    background: var(--sheet);
  }
  .photo {
    flex: 0 0 auto;
    width: 40%;
    min-width: 0;
    align-self: stretch;
    cursor: pointer;
    display: block;
    padding: 0;
    border: none;
    background: var(--ink);
  }
  .photo img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }
  .photo.empty {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--sheet);
    font-family: var(--font-code);
    font-size: 12px;
    cursor: default;
  }

  /* DATA PANEL: the aligned 5-column grid. */
  .panel {
    flex: 1;
    display: grid;
    grid-template-columns: minmax(6rem, 1.5fr) repeat(4, minmax(0, 1fr));
    grid-template-rows: auto 1fr auto;
    border-left: 1px solid var(--ink);
    font-family: var(--font-code);
    min-width: 0;
  }

  .label {
    font-size: 9px;
    font-weight: 700;
    color: var(--ink-2);
    letter-spacing: 0.08em;
    margin-bottom: 4px;
  }
  .val {
    font-size: 18px;
    font-weight: 700;
    color: var(--ink);
  }
  .val.sm {
    font-size: 14px;
  }
  .unit {
    font-size: 10px;
    color: var(--ink-2);
  }
  .sub {
    font-size: 9px;
    color: var(--ink-2);
    font-weight: 600;
  }

  /* Row 1 cells. */
  .r1 {
    padding: 0.7rem;
    border-bottom: 1px solid var(--ink);
    background: var(--sheet);
  }
  .div-thin {
    border-right: 1px solid var(--stroke);
  }
  .c-time {
    border-right: 1px solid var(--ink);
  }
  .time-big {
    font-size: 24px;
    font-weight: 900;
    color: var(--ink);
    line-height: 1;
  }
  .period {
    font-size: 11px;
    font-weight: 700;
    color: var(--ink-2);
    margin-left: 4px;
  }
  /* Row 2, column 1: optics / nav / bearing stack. */
  .col1 {
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--ink);
    border-bottom: 1px solid var(--ink);
    background: var(--band);
  }
  .optics {
    padding: 12px;
    border-bottom: 1px solid var(--ink);
    background: var(--band);
  }
  .optics-line {
    font-size: 14px;
    font-weight: 700;
    color: var(--ink);
  }
  .optics-sub {
    font-size: 11px;
    font-weight: 600;
    color: var(--ink-2);
    margin-top: 2px;
  }
  .nav {
    border-bottom: 1px solid var(--ink);
    background: var(--sheet);
  }
  .navrow {
    display: flex;
    height: 56px;
  }
  .navstep {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--sheet);
    border: none;
    color: var(--ink);
    cursor: pointer;
  }
  .navstep.border-r {
    border-right: 1px solid var(--ink);
  }
  .navstep svg {
    width: 24px;
    height: 24px;
  }
  .navstep:hover:not(:disabled) {
    background: var(--ink);
    color: var(--sheet);
  }
  .navstep:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }
  /* Counter shares the panel ground and keeps its day-colour rule. */
  .counter {
    font-size: 11px;
    font-weight: 700;
    color: var(--ink);
    text-align: center;
    padding: 8px;
    border-top: 1px solid var(--ink);
  }
  .bearing {
    flex-grow: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    padding: 12px;
    background: var(--sheet);
  }
  .bearing .arrow {
    font-size: 36px;
    line-height: 1;
    color: var(--ink);
  }
  .bearing-deg {
    font-size: 11px;
    font-weight: 700;
    color: var(--ink-2);
    margin-top: 4px;
  }

  /* Row 2 elevation profile, spanning columns 2-5. */
  .elev-profile {
    grid-column: span 4;
    padding: 12px 16px;
    border-bottom: 1px solid var(--ink);
    display: flex;
    flex-direction: column;
    background: var(--sheet);
  }
  .elev-profile .label {
    margin-bottom: 8px;
  }
  .elev-inner {
    flex-grow: 1;
    position: relative;
    min-height: 120px;
  }

  .panel > * {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  /* Row 3 cells. */
  .cell-position {
    padding: 10px 12px;
    border-right: 1px solid var(--ink);
    background: var(--sheet);
  }
  .coords {
    font-size: 10px;
    font-weight: 600;
    color: var(--ink);
    line-height: 1.6;
  }
  .cell-stat {
    padding: 10px;
    background: var(--band);
    border-bottom: 1px solid var(--stroke);
  }
  .km-val {
    font-size: 16px;
    font-weight: 900;
    color: var(--ink);
  }
  .small {
    font-size: 10px;
    color: var(--ink-2);
  }
  .stat-val {
    font-size: 14px;
    font-weight: 900;
    color: var(--ink);
  }
  .stat-val.up {
    color: var(--tone-ram);
  }
  .stat-val.down {
    color: var(--tone-disk);
  }

  /* Fullscreen photo overlay. */
  .fs {
    position: fixed;
    inset: 0;
    z-index: 1100;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--sheet);
    border: none;
    padding: 24px;
    cursor: zoom-out;
  }
  .fs img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }

  /* Responsive: stack the triptych and reflow the panel to two columns. */
  @media (max-width: 1200px) {
    .triptych {
      flex-direction: column;
    }
    .photo {
      width: 100%;
      height: auto;
      max-height: 60vh;
      aspect-ratio: 1;
    }
    .photo img {
      max-height: 60vh;
    }
    .panel {
      border-left: none;
      border-top: 1px solid var(--ink);
      grid-template-columns: 1fr 1fr;
      grid-template-rows: none;
    }
    .c-time,
    .col1,
    .elev-profile,
    .cell-position {
      grid-column: 1 / -1;
    }
    .c-time,
    .col1,
    .cell-position {
      border-right: none;
    }
  }
</style>
