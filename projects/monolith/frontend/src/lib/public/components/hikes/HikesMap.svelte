<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import {
    groupWindowsByDay,
    windowFields,
  } from "$lib/public/hikes/filters.js";

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
  // red (hard), keeping the ships heat ramp's orange/red hot end so the two
  // maps still rhyme. Green keeps the gentle majority (≈67% of the corpus)
  // reading calm instead of alarming-hot. Hardcoded hex (data-viz ramp stops,
  // not design-system surface tokens). Keep in sync with the .legend-bar
  // gradient below.
  const EFFORT_RAMP = [
    { at: 0.0, color: "#15a34a" }, // green, gentlest
    { at: 0.25, color: "#84c91e" }, // lime
    { at: 0.5, color: "#ffcb1f" }, // golden yellow
    { at: 0.75, color: "#ff6a00" }, // orange (ships)
    { at: 1.0, color: "#ff0019" }, // red (ships), most strenuous
  ];

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

  // OpenFreeMap needs no API key (same hosted liberty style as /app/ships, so
  // the two maps read as siblings).
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  const SOURCE_ID = "walks";
  const LAYER_ID = "walks";

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let layerReady = false;
  // Signature of the coordinate set we last framed. The filtered `walks` prop
  // changes as the user adjusts filters, so (unlike ShipsMap's stable live
  // snapshot) we re-fit whenever the set of plotted coordinates actually
  // changes, but skip unrelated re-renders that leave the points identical.
  let lastFitKey = null;

  // uuid -> walk row, so a marker click can recover the full record.
  const index = new Map();

  // Map init failed: either the (~1 MB) maplibre chunk did not load or the
  // browser refused a WebGL context. The map is the only view of the walks, so
  // without this the page renders its controls over a blank rectangle and a
  // dead map is indistinguishable from a slow one.
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

  // Marker stroke comes from the design tokens; the fill is the effort ramp
  // (see EFFORT_RAMP). A paper stroke keeps every dot legible over the
  // basemap's greens and water regardless of its ramp colour.
  function palette() {
    const s = getComputedStyle(document.documentElement);
    return {
      ink: s.getPropertyValue("--ink").trim(),
      paper: s.getPropertyValue("--paper").trim(),
    };
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
  // we show just that day; in "Any" mode we stack EVERY doable day (a long
  // day-separated list, like the stars site card) so the card represents the
  // week ahead rather than only the next good day. Each group is {key, label,
  // rows} and only includes days that are both doable and have hourly rows.
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
    let destroyed = false;

    (async () => {
      maplibregl = (await import("maplibre-gl")).default;
      if (destroyed) return;

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
      // Same handling as ShipsMap.
      let userOpenedAttrib = false;
      const collapseAttrib = () => {
        if (userOpenedAttrib) return;
        const attrib = mapContainer.querySelector(".maplibregl-ctrl-attrib");
        attrib?.removeAttribute("open");
        attrib?.classList.remove("maplibregl-compact-show");
      };
      mapContainer.addEventListener("click", (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      });
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

      map.on("load", () => {
        const pal = palette();

        map.addSource(SOURCE_ID, { type: "geojson", data: emptyFC() });
        map.addLayer({
          id: LAYER_ID,
          type: "circle",
          source: SOURCE_ID,
          paint: {
            // Fill by effort: violet (gentle) -> red (strenuous), interpolated
            // over the score. Paper stroke keeps every dot legible on any
            // basemap colour; harder walks read a touch larger.
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
            "circle-stroke-color": pal.paper,
          },
        });
        layerReady = true;

        map.on("click", LAYER_ID, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          const walk = index.get(f.properties.uuid);
          if (walk) selectWalk(walk);
        });
        map.on("mouseenter", LAYER_ID, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", LAYER_ID, () => {
          map.getCanvas().style.cursor = "";
        });

        syncWalks();

        // Deep link (?walk=): open that route's card on load and fly to it so a
        // shared link lands on the marker. Only if it survived the active
        // filters (it is in the plotted set / index); otherwise there is no
        // marker to show. fitToWalks already ran in syncWalks, so override the
        // frame with a closer view centred on the selected walk.
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
      // Unhandled, both leave the container blank forever with nothing logged.
      // Errors raised later from map event callbacks are outside this chain.
      if (destroyed) return;
      console.error("hikes map failed to initialise", err);
      mapError = true;
    });

    // Teardown runs whether or not init got far enough to build the map, so a
    // throw partway through cannot strand a live map instance.
    return () => {
      destroyed = true;
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
    <p class="map-error" role="status">
      The map could not be loaded. It needs WebGL, which some browsers restrict.
      Try reloading, or open this page in another browser.
    </p>
  {/if}

  {#if selected}
    <aside class="card">
      <button
        class="card-close"
        onclick={closeCard}
        aria-label="Close walk card">&times;</button
      >
      <h2 class="card-name">
        <a href={selected.url} target="_blank" rel="noopener"
          >{selected.name}<span class="card-name-arrow" aria-hidden="true"
            >&nearr;</span
          ></a
        >
      </h2>
      <dl class="card-stats">
        <div>
          <dt>Distance</dt>
          <dd>{selected.distance_km} km</dd>
        </div>
        <div>
          <dt>Ascent</dt>
          <dd>{selected.ascent_m} m</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{fmtDuration(selected.duration_h)}</dd>
        </div>
      </dl>
      {#if selectedDetail?.summary}
        <p class="card-summary">{selectedDetail.summary}</p>
      {/if}

      {#if detailLoading}
        <p class="card-empty">Loading forecast…</p>
      {:else if detailError}
        <p class="card-empty">Couldn't load the forecast. Try again.</p>
      {:else if cardGroups.length}
        <p class="eyebrow card-windows-title">{cardEyebrow}</p>
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
                   when stacking several days; with one section the eyebrow above
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
        <p class="card-empty">
          Not enough good daylight hours for this {fmtDuration(
            selected.duration_h,
          )} walk on any day in the forecast.
        </p>
      {:else}
        <p class="card-empty">No viable weather windows in the forecast.</p>
      {/if}
    </aside>
  {/if}

  <!-- Effort legend: the colour ramp that tints the markers. Pointless next to
       a map that never came up, so it goes with it. -->
  {#if !mapError}
    <div class="legend">
      <span class="legend-title">Effort</span>
      <span class="legend-bar" aria-hidden="true"></span>
      <span class="legend-ends"><span>Gentle</span><span>Strenuous</span></span>
      <span class="legend-note">distance + ascent + time</span>
    </div>
  {/if}
</div>

<style>
  .map-wrap {
    position: absolute;
    inset: 0;
    overflow: hidden;
    background: var(--cream);
  }

  .map {
    position: absolute;
    inset: 0;
  }

  /* Inset the bottom-right control stack past device safe areas (same reasons
     as ShipsMap: home indicator + landscape notch). */
  .map :global(.maplibregl-ctrl-bottom-right) {
    bottom: env(safe-area-inset-bottom, 0);
    right: env(safe-area-inset-right, 0);
  }

  /* Each zoom button is its own square that lifts off (neobrutalist), matching
     the ships map and the homepage buttons. */
  .map :global(.maplibregl-ctrl-group) {
    border: none;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
    overflow: visible;
  }

  .map :global(.maplibregl-ctrl-group button) {
    border: 2px solid var(--ink);
    background: var(--paper);
    transition:
      transform 110ms ease,
      box-shadow 110ms ease;
  }

  .map :global(.maplibregl-ctrl-group button + button) {
    margin-top: 6px;
  }

  .map :global(.maplibregl-ctrl-group button:hover),
  .map :global(.maplibregl-ctrl-group button:focus-visible),
  .map :global(.maplibregl-ctrl-group button:active) {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
    background: var(--paper);
    position: relative;
    z-index: 1;
  }

  /* Compact attribution: a single square "i" toggle matching the zoom stack. */
  .map :global(.maplibregl-ctrl-attrib.maplibregl-compact) {
    min-height: 24px;
    padding-block: 0;
    background: transparent;
    box-shadow: none;
  }

  .map :global(.maplibregl-ctrl-attrib-button) {
    width: 24px;
    height: 24px;
    border: 2px solid var(--ink);
    border-radius: 0;
    background-color: var(--paper);
  }

  .map :global(.maplibregl-ctrl-attrib.maplibregl-compact-show) {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: 0;
  }

  .map :global(.maplibregl-ctrl-attrib-inner) {
    font-family: var(--mono);
    font-size: 10px;
  }

  /* Map-failed notice: centred in the dead map area, same paper/ink treatment
     as the legend and card so it reads as part of the page, not a crash. */
  .map-error {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    z-index: 3;
    max-width: min(320px, calc(100% - 32px));
    padding: 14px 16px;
    background: var(--paper);
    border: 2px solid var(--ink);
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.5;
    color: var(--ink);
    text-align: center;
  }

  /* Walk detail card: a hard-shadow paper card (neobrutalist), top-right and
     clear of the global app chrome. */
  /* Flat sharp-bordered overlay, matching the ships map overlays (no
     border-radius, no drop shadow, paper bg) rather than the global card-hard. */
  .card {
    position: absolute;
    top: 16px;
    right: 16px;
    width: 320px;
    max-width: calc(100% - 32px);
    max-height: calc(100% - 32px);
    overflow-y: auto;
    padding: 18px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .card-close {
    position: absolute;
    top: 8px;
    right: 10px;
    background: none;
    border: none;
    font-family: var(--mono);
    font-size: 22px;
    line-height: 1;
    cursor: pointer;
    color: var(--ink);
  }

  .card-name {
    font-family: var(--serif);
    font-size: 26px;
    line-height: 1.05;
    margin: 2px 28px 12px 0;
    word-break: break-word;
  }

  /* The name is the link to WalkHighlands (no separate footer link). */
  .card-name a {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-decoration-skip-ink: none;
    text-underline-offset: 3px;
    transition: background 140ms ease;
  }

  .card-name a:hover,
  .card-name a:focus-visible {
    background: linear-gradient(transparent 62%, var(--accent) 62%);
    text-decoration-color: var(--ink);
  }

  .card-name-arrow {
    font-size: 0.62em;
    margin-left: 3px;
    white-space: nowrap;
    vertical-align: 0.18em;
  }

  /* Stats sit in a framed box so the grey labels read as a distinct block
     rather than floating low-contrast on the paper. */
  .card-stats {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
    padding: 10px 12px;
    background: var(--cream);
    border: 2px solid var(--ink);
  }

  .card-stats > div {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .card-stats dt {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .card-stats dd {
    font-family: var(--mono);
    font-size: 15px;
    font-weight: 700;
  }

  .card-summary {
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.45;
    color: var(--ink-2);
    margin-bottom: 14px;
  }

  /* Highlighted header bar so "Next viable: <day>" reads as the answer, not a
     faint grey eyebrow (overrides .eyebrow's muted colour). */
  .card-windows-title {
    display: inline-block;
    margin: 0 0 10px;
    padding: 4px 8px;
    background: var(--accent);
    color: var(--ink);
    border: 2px solid var(--ink);
  }

  .card-windows {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 11px;
    margin-bottom: 14px;
  }

  .card-windows th {
    text-align: left;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    border-bottom: 2px solid var(--ink);
    padding: 4px 6px 4px 0;
  }

  /* Day section header inside the stacked "Any" list: a full-width label that
     divides each day's hours. A top rule separates day groups; it drops the
     column row's heavy underline (that belongs to the Time/Temp/... header). */
  .card-windows .day-head th {
    border-top: 2px solid var(--ink);
    border-bottom: 1px solid var(--rule-2);
    padding: 11px 0 5px;
    color: var(--ink);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  /* The first day header sits right under the column row, so it needs no extra
     top rule. */
  .card-windows .day-head:first-of-type th {
    border-top: none;
    padding-top: 8px;
  }

  .card-windows td {
    padding: 4px 6px 4px 0;
    border-bottom: 1px dashed var(--rule-2);
  }

  .card-empty {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    margin-bottom: 14px;
  }

  /* Effort legend, bottom-left, clear of the bottom-right zoom + attribution. */
  .legend {
    position: absolute;
    left: 16px;
    bottom: 16px;
    z-index: 4;
    display: flex;
    flex-direction: column;
    gap: 4px;
    width: 184px;
    padding: 10px 12px;
    background: var(--paper);
    border: 2px solid var(--ink);
    font-family: var(--mono);
  }

  .legend-title {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  /* Gradient mirrors EFFORT_RAMP (kept in sync by hand: CSS can't read the JS
     const). */
  .legend-bar {
    height: 10px;
    border: 2px solid var(--ink);
    background: linear-gradient(
      to right,
      #15a34a,
      #84c91e,
      #ffcb1f,
      #ff6a00,
      #ff0019
    );
  }

  .legend-ends {
    display: flex;
    justify-content: space-between;
    font-size: 9px;
    letter-spacing: 0.04em;
    color: var(--ink-3);
  }

  .legend-note {
    font-size: 9px;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  @media (max-width: 640px) {
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
      max-height: 70vh;
      border-width: 2px 0 0;
      box-shadow: none;
      border-radius: 0;
      padding: 20px 18px calc(20px + env(safe-area-inset-bottom));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .map :global(.maplibregl-ctrl-group button) {
      transition: none;
    }
  }
</style>
