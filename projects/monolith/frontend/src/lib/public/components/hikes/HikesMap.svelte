<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
  import Button from "@homelab/design-system/components/Button.svelte";
  import {
    groupWindowsByDay,
    windowFields,
  } from "$lib/public/hikes/filters.js";
  import {
    applyMapPalette,
    readMapPalette,
    WALKS_LAYER,
  } from "$lib/public/hikes/mapTheme.js";

  // `walks` is the filtered set to plot; clicking a marker opens the card.
  // `selectedDay` is the parent's chosen day chip (YYYY-MM-DD) or null for
  // "any"; the marker card prefers that day's windows when it is set.
  // `maxima` holds the corpus p95 of duration/ascent/distance, the ceilings the
  // effort score normalizes against (passed in so the colour is stable as the
  // filtered set changes).
  // `initialUuid` is a deep-linked walk to open on load (from ?walk=); we open
  // it once the layer is ready and fly to it. `onSelectWalk(uuid|null)` lifts the
  // open/closed card up to the page so it can mirror the selection to the URL.
  let {
    walks = [],
    selectedDay = null,
    maxima = { duration: 1, ascent: 1, distance: 1 },
    initialUuid = null,
    onSelectWalk = () => {},
  } = $props();

  // Effort ramp: the universal trail-difficulty convention, green (easy) ->
  // red (hard). Green keeps the gentle majority (about two thirds of the
  // corpus) reading calm instead of alarming-hot. These are data-ramp stops,
  // not surface tokens, so they stay fixed in both schemes; the legend bar
  // below draws the same stops.
  const EFFORT_RAMP = [
    { at: 0.0, color: "#15a34a" }, // green, gentlest
    { at: 0.25, color: "#84c91e" }, // lime
    { at: 0.5, color: "#ffcb1f" }, // golden yellow
    { at: 0.75, color: "#ff6a00" }, // orange
    { at: 1.0, color: "#ff0019" }, // red, most strenuous
  ];
  const LEGEND_GRADIENT = `linear-gradient(to right, ${EFFORT_RAMP.map(
    (s) => s.color,
  ).join(", ")})`;

  // Effort score in [0,1]: equal-weight blend of duration, ascent and distance,
  // each normalized against the corpus p95 and clamped. Triple-counts effort on
  // purpose (the user asked for "time + ascent + length"); they correlate, so
  // the blend just sharpens the ordering.
  function effortScore(walk) {
    const norm = (v, max) => Math.min(1, Math.max(0, (v ?? 0) / (max || 1)));
    const d = norm(walk.duration_h, maxima.duration);
    const a = norm(walk.ascent_m, maxima.ascent);
    const l = norm(walk.distance_km, maxima.distance);
    return (d + a + l) / 3;
  }

  // WalkHighlands stores duration as fractional hours (e.g. 0.333). Show
  // sub-hour walks in minutes and the rest as hours without a trailing ".0".
  function fmtDuration(hours) {
    if (hours == null) return "";
    if (hours < 1) return `${Math.round(hours * 60)} min`;
    return `${Number(hours.toFixed(1))} h`;
  }

  // Keep the hosted vector style and its geography (the same liberty style as
  // ships and stars). Paint follows the sheet in place after load and on every
  // explicit or system scheme change.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  const SOURCE_ID = "walks";

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let layerReady = false;
  // Signature of the coordinate set we last framed. The filtered `walks` prop
  // changes as the user adjusts filters, so we re-fit whenever the set of
  // plotted coordinates actually changes, but skip unrelated re-renders that
  // leave the points identical.
  let lastFitKey = null;

  // uuid -> walk row, so a marker click can recover the full record.
  const index = new Map();

  // Map init failed: either the maplibre chunk did not load or the browser
  // refused a WebGL context. The map is the only view of the walks, so without
  // this a dead map is indistinguishable from a slow one.
  let mapError = $state(false);

  let selected = $state(null); // selected walk (light list row), or null
  // The light corpus omits summary + hourly windows; the card fetches them per
  // walk on selection from /app/hikes/walk/{uuid}. detail = {summary, windows}.
  let selectedDetail = $state(null);
  let detailLoading = $state(false);
  let detailError = $state(false);
  // Guards against an out-of-order resolution: a fast second click must win.
  let detailToken = 0;

  async function selectWalk(walk) {
    selected = walk;
    onSelectWalk(walk.uuid); // lift to the page so it mirrors ?walk=
    selectedDetail = null;
    detailError = false;
    detailLoading = true;
    const token = ++detailToken;
    try {
      const res = await fetch(
        `/app/hikes/walk/${encodeURIComponent(walk.uuid)}`,
      );
      if (!res.ok) throw new Error(`status ${res.status}`);
      const detail = await res.json();
      if (token !== detailToken) return; // a newer selection superseded this one
      selectedDetail = detail;
    } catch {
      if (token !== detailToken) return;
      detailError = true;
    } finally {
      if (token === detailToken) detailLoading = false;
    }
  }

  function emptyFC() {
    return { type: "FeatureCollection", features: [] };
  }

  function validLatLon(lat, lon) {
    if (lat == null || lon == null) return false;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return false;
    if (lat === 0 && lon === 0) return false;
    return true;
  }

  function buildFeatures() {
    const features = [];
    for (const walk of walks) {
      if (!validLatLon(walk.latitude, walk.longitude)) continue;
      features.push({
        type: "Feature",
        id: walk.uuid,
        geometry: {
          type: "Point",
          coordinates: [walk.longitude, walk.latitude],
        },
        properties: { uuid: walk.uuid, score: effortScore(walk) },
      });
    }
    return { type: "FeatureCollection", features };
  }

  function pushData() {
    const src = map?.getSource(SOURCE_ID);
    if (src) src.setData(buildFeatures());
  }

  function fitToWalks() {
    const pts = walks.filter((w) => validLatLon(w.latitude, w.longitude));
    // No plottable points: leave the view as-is (do not crash, do not reset the
    // coordinate signature, so the next non-empty set re-frames).
    if (!pts.length) return;
    // Only re-fit when the coordinate set genuinely changed; an unrelated
    // re-render with the same points should not re-animate the viewport.
    const key = pts
      .map((w) => `${w.longitude},${w.latitude}`)
      .sort()
      .join("|");
    if (key === lastFitKey) return;
    const b = new maplibregl.LngLatBounds();
    for (const w of pts) b.extend([w.longitude, w.latitude]);
    map.fitBounds(b, { padding: 56, maxZoom: 11, duration: 0 });
    lastFitKey = key;
  }

  function syncWalks() {
    if (!map || !layerReady) return;
    index.clear();
    for (const w of walks) index.set(w.uuid, w);
    pushData();
    fitToWalks();
    // If the open card's walk fell out of the filtered set, close it.
    if (selected && !index.has(selected.uuid)) closeCard();
  }

  function closeCard() {
    selected = null;
    onSelectWalk(null); // clear ?walk= on the page
    selectedDetail = null;
    detailError = false;
    detailLoading = false;
    detailToken++; // invalidate any in-flight detail fetch
  }

  // Window rows for the selected walk's card, grouped by UK-local day. Windows
  // come from the per-walk detail fetch (selectedDetail), not the light list.
  let cardDays = $derived(
    selectedDetail ? groupWindowsByDay(selectedDetail) : {},
  );
  let cardDayKeys = $derived(Object.keys(cardDays).sort());
  // The days the server judged DOABLE for this walk's length (duration-aware: a
  // long-enough run of good hours, see router._doable_days). The card honours it
  // so a walk only ever lists days it actually fits, not every day with a single
  // good hour.
  let doableDays = $derived(new Set(selected?.viable_days ?? []));

  function dayLabel(key) {
    return new Date(`${key}T12:00:00Z`).toLocaleDateString("en-GB", {
      weekday: "long",
      day: "numeric",
      month: "short",
      timeZone: "Europe/London",
    });
  }

  // The day-separated sections the card shows. With a specific day chip selected
  // we show just that day; in "Any" mode we stack EVERY doable day so the card
  // represents the week ahead rather than only the next good day. Each group is
  // {key, label, rows} and only includes days that are both doable and have
  // hourly rows.
  let cardGroups = $derived.by(() => {
    if (!selectedDetail) return [];
    const days =
      selectedDay && doableDays.has(selectedDay)
        ? [selectedDay]
        : cardDayKeys.filter((d) => doableDays.has(d));
    return days
      .filter((d) => cardDays[d]?.length)
      .map((d) => ({
        key: d,
        label: dayLabel(d),
        rows: cardDays[d].map(windowFields),
      }));
  });
  // One section is labelled "Next viable: <day>"; several read as the week's
  // good days. The in-table day headers carry the per-day labels when stacked.
  let cardEyebrow = $derived(
    cardGroups.length === 1
      ? `Next viable: ${cardGroups[0].label}`
      : "Good days ahead",
  );

  function fmtTime(date) {
    return date.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/London",
    });
  }

  // Repaint whenever the filtered walk set or the effort ceilings change.
  $effect(() => {
    void walks;
    void maxima;
    if (map && layerReady) syncWalks();
  });

  onMount(() => {
    let cleanup = () => {};
    let destroyed = false;

    (async () => {
      maplibregl = await import("maplibre-gl");
      if (destroyed) return;
      // Vite must bundle the worker's shared imports too. The package-relative
      // default URL otherwise points at a missing asset in a production build.
      maplibregl.setWorkerUrl(workerUrl);

      map = new maplibregl.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
        // Scotland: the whole walk corpus is up north, so frame there until the
        // first fitBounds lands.
        center: [-4.2, 57.0],
        zoom: 5.5,
        attributionControl: { compact: true },
      });
      map.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );

      // Collapse the compact attribution on every update so it never paints as
      // a full-width credit bar; leave it alone once the user taps it open.
      // Same handling as ShipsMap / StarsMap.
      let userOpenedAttrib = false;
      const collapseAttrib = () => {
        if (userOpenedAttrib) return;
        const attrib = mapContainer.querySelector(".maplibregl-ctrl-attrib");
        attrib?.removeAttribute("open");
        attrib?.classList.remove("maplibregl-compact-show");
      };
      const trackAttribution = (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      };
      mapContainer.addEventListener("click", trackAttribution);
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

      // The sheet's scheme is the map's scheme: repaint in place on the
      // reader's toggle and on a system change, without touching the camera,
      // sources or the open card.
      const restyle = () => {
        if (map && layerReady)
          applyMapPalette(map, readMapPalette(mapContainer));
      };
      const scheme = new MutationObserver(restyle);
      scheme.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
      const media = window.matchMedia("(prefers-color-scheme: dark)");
      media.addEventListener("change", restyle);
      const resize = new ResizeObserver(() => map?.resize());
      resize.observe(mapContainer);

      // MapLibre supplies keyboard pan and zoom. Enter selects the walk under
      // the map centre; Escape closes its card without moving focus.
      const canvas = map.getCanvas();
      canvas.setAttribute("aria-label", "Hill walks");
      canvas.setAttribute(
        "aria-description",
        "Arrow keys pan, plus and minus zoom, Enter selects a walk at the map centre, Escape closes its card.",
      );
      const keyboard = (event) => {
        if (event.key === "Escape") closeCard();
        if (event.key !== "Enter" || !layerReady) return;
        event.preventDefault();
        const point = map.project(map.getCenter());
        const feature = map.queryRenderedFeatures(point, {
          layers: [WALKS_LAYER],
        })[0];
        const walk = feature && index.get(feature.properties.uuid);
        if (walk) selectWalk(walk);
      };
      canvas.addEventListener("keydown", keyboard);

      cleanup = () => {
        scheme.disconnect();
        media.removeEventListener("change", restyle);
        resize.disconnect();
        canvas.removeEventListener("keydown", keyboard);
        mapContainer.removeEventListener("click", trackAttribution);
      };

      map.on("load", () => {
        const pal = readMapPalette(mapContainer);

        map.addSource(SOURCE_ID, { type: "geojson", data: emptyFC() });
        map.addLayer({
          id: WALKS_LAYER,
          type: "circle",
          source: SOURCE_ID,
          paint: {
            // Fill by effort, interpolated over the score. A sheet-coloured
            // stroke keeps every dot legible on the basemap in either
            // scheme; harder walks read a touch larger.
            "circle-color": [
              "interpolate",
              ["linear"],
              ["get", "score"],
              ...EFFORT_RAMP.flatMap((s) => [s.at, s.color]),
            ],
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["get", "score"],
              0,
              5,
              1,
              8,
            ],
            "circle-stroke-width": 2,
            "circle-stroke-color": pal.sheet,
          },
        });
        layerReady = true;
        applyMapPalette(map, pal);

        map.on("click", WALKS_LAYER, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          const walk = index.get(f.properties.uuid);
          if (walk) selectWalk(walk);
        });
        map.on("mouseenter", WALKS_LAYER, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", WALKS_LAYER, () => {
          map.getCanvas().style.cursor = "";
        });

        syncWalks();

        // Deep link (?walk=): open that route's card on load and fly to it so a
        // shared link lands on the marker. Only if it survived the active
        // filters (it is in the plotted set / index); otherwise there is no
        // marker to show.
        if (initialUuid) {
          const walk = index.get(initialUuid);
          if (walk && validLatLon(walk.latitude, walk.longitude)) {
            selectWalk(walk);
            map.flyTo({
              center: [walk.longitude, walk.latitude],
              zoom: Math.max(map.getZoom(), 9),
              duration: 0,
            });
          }
        }
      });
    })().catch((err) => {
      // Rejects if the maplibre chunk failed to load or the Map constructor
      // threw, which it does when the browser hands out no WebGL context.
      if (destroyed) return;
      console.error("hikes map failed to initialise", err);
      mapError = true;
    });

    // Teardown runs whether or not init got far enough to build the map, so a
    // throw partway through cannot strand a live map instance.
    return () => {
      destroyed = true;
      cleanup();
      index.clear();
      map?.remove();
      map = null;
      layerReady = false;
      lastFitKey = null;
    };
  });
</script>

<div class="map-wrap" class:panel-open={selected}>
  <div class="map" bind:this={mapContainer}></div>

  {#if mapError}
    <p class="map-error edition" role="status">
      The map could not be loaded. It needs WebGL, which some browsers restrict.
      Try reloading, or open this page in another browser.
    </p>
  {/if}

  {#if selected}
    <aside class="card edition" aria-label={selected.name}>
      <header class="card-head ed-head">
        <p class="card-title">
          <a href={selected.url} target="_blank" rel="noopener"
            >{selected.name} ↗</a
          >
        </p>
        <Button
          class="card-close"
          variant="quiet"
          onclick={closeCard}
          aria-label="Close walk card">&times;</Button
        >
      </header>
      <dl class="card-stats spec">
        <div class="row">
          <dt>Distance</dt>
          <dd>{selected.distance_km} km</dd>
        </div>
        <div class="row">
          <dt>Ascent</dt>
          <dd>{selected.ascent_m} m</dd>
        </div>
        <div class="row">
          <dt>Duration</dt>
          <dd>{fmtDuration(selected.duration_h)}</dd>
        </div>
      </dl>
      {#if selectedDetail?.summary}
        <p class="card-summary">{selectedDetail.summary}</p>
      {/if}

      {#if detailLoading}
        <p class="card-note" role="status">Loading forecast…</p>
      {:else if detailError}
        <p class="card-note" role="status">
          Couldn't load the forecast. Try again.
        </p>
      {:else if cardGroups.length}
        <p class="card-windows-title">{cardEyebrow}</p>
        <table class="card-windows">
          <thead>
            <tr>
              <th>Time</th>
              <th>Temp</th>
              <th>Rain</th>
              <th>Wind</th>
              <th>Cloud</th>
            </tr>
          </thead>
          <tbody>
            {#each cardGroups as g (g.key)}
              <!-- A full-width day header separates each day's hours. Shown only
                   when stacking several days; with one section the title above
                   already names the day. -->
              {#if cardGroups.length > 1}
                <tr class="day-head">
                  <th colspan="5" scope="colgroup">{g.label}</th>
                </tr>
              {/if}
              {#each g.rows as w (w.ts)}
                <tr>
                  <td>{fmtTime(w.date)}</td>
                  <td>{Math.round(w.temp_c)}C</td>
                  <td>{w.precip_mm.toFixed(1)}mm</td>
                  <td>{Math.round(w.wind_kmh)}km/h</td>
                  <td>{Math.round(w.cloud_pct)}%</td>
                </tr>
              {/each}
            {/each}
          </tbody>
        </table>
      {:else if cardDayKeys.length}
        <p class="card-note">
          Not enough good daylight hours for this {fmtDuration(
            selected.duration_h,
          )} walk on any day in the forecast.
        </p>
      {:else}
        <p class="card-note">No viable weather windows in the forecast.</p>
      {/if}
    </aside>
  {/if}

  <!-- Effort legend: the colour ramp that tints the markers. Pointless next to
       a map that never came up, so it goes with it. -->
  {#if !mapError}
    <div class="legend">
      <span class="legend-title">Effort</span>
      <span class="legend-ramp" style:background={LEGEND_GRADIENT}></span>
      <span class="legend-ends"><span>gentle</span><span>strenuous</span></span>
    </div>
  {/if}
</div>

<style>
  .map-wrap {
    /* Geography uses neutral sheet roles so it cannot compete with the effort
       ramp, the one coloured thing on the map. */
    --hikes-sheet: var(--sheet);
    --hikes-ink: var(--ink);
    --hikes-ink-2: var(--ink-2);
    --hikes-stroke: var(--stroke);
    --hikes-band: var(--band);
    --hikes-water: color-mix(in srgb, var(--tone-cache) 10%, var(--sheet));
    position: relative;
    background: var(--sheet);
    min-width: 0;
  }

  .map {
    height: clamp(32rem, calc(100dvh - 7rem), 54rem);
    background: var(--sheet);
  }

  /* MapLibre's controls keep their behavior; their surfaces use sheet roles. */
  .map :global(.maplibregl-ctrl-bottom-right) {
    bottom: env(safe-area-inset-bottom, 0);
    right: env(safe-area-inset-right, 0);
  }

  .map :global(.maplibregl-ctrl-group) {
    border: 1px solid var(--ink);
    border-radius: 0;
    background: var(--sheet);
    box-shadow: none;
    overflow: visible;
  }

  .map :global(.maplibregl-ctrl-group button) {
    width: 44px;
    height: 44px;
    background: var(--sheet);
  }

  .map :global(.maplibregl-ctrl-group button + button) {
    border-top: 1px solid var(--stroke);
  }

  .map :global(.maplibregl-ctrl-group button:hover) {
    background: var(--band);
  }

  /* The stock icons are black bitmap SVGs. Text glyphs inherit ink instead. */
  .map :global(.maplibregl-ctrl-icon) {
    background-image: none;
    display: grid;
    place-items: center;
    font: 1.5rem var(--font-code);
    color: var(--ink);
  }

  .map :global(.maplibregl-ctrl-zoom-in .maplibregl-ctrl-icon)::before {
    content: "+";
  }

  .map :global(.maplibregl-ctrl-zoom-out .maplibregl-ctrl-icon)::before {
    content: "−";
  }

  .map :global(.maplibregl-ctrl-attrib) {
    background: var(--sheet);
    color: var(--ink-2);
    border: 1px solid var(--stroke);
    border-radius: 0;
    box-shadow: none;
    font: 0.65rem var(--font-code);
  }

  .map :global(.maplibregl-ctrl-attrib a) {
    color: var(--ink-2);
  }

  .map :global(.maplibregl-ctrl-attrib-button) {
    border-radius: 0;
    background: var(--sheet);
    color: var(--ink);
  }

  .map :global(.maplibregl-ctrl-attrib-button)::before {
    content: "i";
    font: 1rem var(--font-code);
  }

  .map :global(.maplibregl-canvas:focus-visible) {
    outline-offset: -3px;
  }

  /* Map-failed notice: centred in the dead map area, drawn like any panel so
     it reads as part of the page, not a crash. */
  .map-error {
    position: absolute;
    top: 50%;
    left: 50%;
    z-index: 3;
    max-width: min(20rem, calc(100% - 2rem));
    margin: 0;
    padding: 0.8rem 1rem;
    background: var(--sheet);
    font: 0.75rem/1.6 var(--font-code);
    color: var(--ink-2);
    text-align: center;
    transform: translate(-50%, -50%);
  }

  /* Walk card: one panel over the map, top right, clear of the controls. */
  .card {
    position: absolute;
    z-index: 3;
    top: 1rem;
    right: 1rem;
    width: 18rem;
    max-width: calc(100% - 2rem);
    max-height: calc(100% - 8rem);
    overflow-y: auto;
    background: var(--sheet);
  }

  .card-head {
    display: flex;
    align-items: center;
    padding: 0.4rem 0.75rem;
    gap: 0.5rem;
  }

  /* The name is the link to WalkHighlands (no separate footer link). */
  .card-title {
    flex: 1;
    margin: 0;
    font: 600 0.8rem/1.4 var(--font-ui);
    color: var(--ink);
  }

  .card-title a {
    color: inherit;
    text-decoration: none;
  }

  .card-title a:hover {
    color: var(--accent-ink);
  }

  .card-head :global(.card-close) {
    flex: none;
    padding: 0;
    font-size: 1.5rem;
    text-decoration: none;
  }

  .card-stats {
    margin: 0;
    border-top: 0;
  }

  .card-stats.spec .row {
    grid-template-columns: 1fr 1fr;
  }

  .card-stats.spec dt,
  .card-stats.spec dd {
    padding: 0.45rem 0.75rem;
  }

  .card-stats.spec dd {
    font-family: var(--font-code);
    font-size: 0.8rem;
  }

  .card-summary {
    margin: 0;
    padding: 0.6rem 0.75rem;
    border-top: 1px solid var(--stroke);
    font-size: 0.82rem;
    line-height: 1.5;
    color: var(--ink);
  }

  .card-note {
    margin: 0;
    padding: 0.6rem 0.75rem;
    border-top: 1px solid var(--stroke);
    font: 0.75rem/1.5 var(--font-code);
    color: var(--ink-2);
  }

  /* "Next viable: <day>" is the answer the card exists for: a band, like the
     head of any record. */
  .card-windows-title {
    margin: 0;
    padding: 0.45rem 0.75rem;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    font: 500 0.72rem/1.5 var(--font-code);
    color: var(--ink);
  }

  .card-windows {
    width: calc(100% - 1.5rem);
    margin: 0.35rem 0.75rem 0.6rem;
    border-collapse: collapse;
    font: 0.75rem var(--font-code);
  }

  .card-windows th,
  .card-windows td {
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--line);
    text-align: left;
  }

  .card-windows thead th {
    color: var(--ink-2);
    font-weight: 500;
    border-bottom-color: var(--stroke);
  }

  /* Day section header inside the stacked "Any" list. */
  .card-windows .day-head th {
    padding-top: 0.8rem;
    color: var(--ink);
    font-weight: 600;
    border-bottom-color: var(--stroke);
  }

  .card-windows td {
    color: var(--ink);
  }

  /* Effort legend, bottom-left, clear of the bottom-right zoom + attribution.
     The ramp is the figure; its ends are labelled on the bar itself. */
  .legend {
    position: absolute;
    left: 1rem;
    bottom: 1rem;
    z-index: 2;
    display: grid;
    gap: 0.3rem;
    width: 10rem;
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    font: 0.68rem var(--font-code);
    color: var(--ink-2);
  }

  .legend-title {
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .legend-ramp {
    display: block;
    height: 0.6rem;
    border: 1px solid var(--stroke);
  }

  .legend-ends {
    display: flex;
    justify-content: space-between;
  }

  @media (max-width: 760px) {
    .map {
      height: clamp(24rem, calc(100dvh - 12rem), 40rem);
    }

    /* Tight on phones: the open card is a bottom sheet, so the legend would
       collide with it; drop the legend there (the colours stay self-evident). */
    .legend {
      display: none;
    }

    /* The card becomes a bottom sheet so the map keeps the upper screen. */
    .card {
      top: auto;
      bottom: 0;
      left: 0;
      right: 0;
      width: auto;
      max-width: none;
      max-height: 70%;
      border-width: 1px 0 0;
    }
  }
</style>
