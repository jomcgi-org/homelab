<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import { groupWindowsByDay, windowFields } from "$lib/public/hikes/filters.js";

  // `walks` is the filtered set to plot; clicking a marker opens the card.
  let { walks = [] } = $props();

  // OpenFreeMap needs no API key (same hosted liberty style as /app/ships, so
  // the two maps read as siblings).
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  const SOURCE_ID = "walks";
  const LAYER_ID = "walks";

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let layerReady = false;
  let didFit = false;

  // uuid -> walk row, so a marker click can recover the full record.
  const index = new Map();

  let selected = $state(null); // selected walk object, or null

  function emptyFC() {
    return { type: "FeatureCollection", features: [] };
  }

  function validLatLon(lat, lon) {
    if (lat == null || lon == null) return false;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return false;
    if (lat === 0 && lon === 0) return false;
    return true;
  }

  // Marker fills come from the design tokens (no hardcoded hex): viable walks
  // get the accent, the rest sit in ink so the eye lands on what is hikeable.
  function palette() {
    const s = getComputedStyle(document.documentElement);
    return {
      ink: s.getPropertyValue("--ink").trim(),
      accent: s.getPropertyValue("--accent").trim(),
      paper: s.getPropertyValue("--paper").trim(),
    };
  }

  // A walk is "viable" for the marker tint when it carries any window at all
  // (the forecast job only stores windows that passed the viability ladder).
  function isViable(walk) {
    return (walk.windows ?? []).length > 0;
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
        properties: { uuid: walk.uuid, viable: isViable(walk) },
      });
    }
    return { type: "FeatureCollection", features };
  }

  function pushData() {
    const src = map?.getSource(SOURCE_ID);
    if (src) src.setData(buildFeatures());
  }

  function fitToWalks() {
    if (didFit) return;
    const pts = walks.filter((w) => validLatLon(w.latitude, w.longitude));
    if (!pts.length) return;
    const b = new maplibregl.LngLatBounds();
    for (const w of pts) b.extend([w.longitude, w.latitude]);
    map.fitBounds(b, { padding: 56, maxZoom: 11, duration: 0 });
    didFit = true;
  }

  function syncWalks() {
    if (!map || !layerReady) return;
    index.clear();
    for (const w of walks) index.set(w.uuid, w);
    pushData();
    fitToWalks();
    // If the open card's walk fell out of the filtered set, close it.
    if (selected && !index.has(selected.uuid)) selected = null;
  }

  function closeCard() {
    selected = null;
  }

  // Stats + window rows for the selected walk's card. Windows are grouped by
  // UK-local day and the next viable day's rows are shown.
  let cardDays = $derived(selected ? groupWindowsByDay(selected) : {});
  let cardDayKeys = $derived(Object.keys(cardDays).sort());
  let cardRows = $derived(
    cardDayKeys.length ? cardDays[cardDayKeys[0]].map(windowFields) : [],
  );
  let cardDayLabel = $derived(
    cardDayKeys.length
      ? new Date(`${cardDayKeys[0]}T12:00:00Z`).toLocaleDateString("en-GB", {
          weekday: "long",
          day: "numeric",
          month: "short",
          timeZone: "Europe/London",
        })
      : "",
  );

  function fmtTime(date) {
    return date.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/London",
    });
  }

  // Repaint whenever the filtered walk set changes.
  $effect(() => {
    void walks;
    if (map && layerReady) syncWalks();
  });

  onMount(() => {
    let cleanup = () => {};
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
            // Viable walks pop in accent; the rest sit in ink. Both carry a
            // paper stroke so they read against the basemap's greens.
            "circle-color": [
              "case",
              ["get", "viable"],
              pal.accent,
              pal.ink,
            ],
            "circle-radius": ["case", ["get", "viable"], 7, 5],
            "circle-stroke-width": 2,
            "circle-stroke-color": pal.paper,
          },
        });
        layerReady = true;

        map.on("click", LAYER_ID, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          const walk = index.get(f.properties.uuid);
          if (walk) selected = walk;
        });
        map.on("mouseenter", LAYER_ID, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", LAYER_ID, () => {
          map.getCanvas().style.cursor = "";
        });

        syncWalks();
      });

      cleanup = () => {
        index.clear();
        map?.remove();
        map = null;
        layerReady = false;
      };
    })();

    return () => {
      destroyed = true;
      cleanup();
    };
  });
</script>

<div class="map-wrap" class:panel-open={selected}>
  <div class="map" bind:this={mapContainer}></div>

  {#if selected}
    <aside class="card card-hard">
      <button class="card-close" onclick={closeCard} aria-label="Close walk card"
        >&times;</button
      >
      <h2 class="card-name">{selected.name}</h2>
      <dl class="card-stats">
        <div><dt>Distance</dt><dd>{selected.distance_km} km</dd></div>
        <div><dt>Ascent</dt><dd>{selected.ascent_m} m</dd></div>
        <div><dt>Duration</dt><dd>{selected.duration_h} h</dd></div>
      </dl>
      {#if selected.summary}
        <p class="card-summary">{selected.summary}</p>
      {/if}

      {#if cardRows.length}
        <p class="eyebrow card-windows-title">Next viable: {cardDayLabel}</p>
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
            {#each cardRows as w (w.ts)}
              <tr>
                <td>{fmtTime(w.date)}</td>
                <td>{Math.round(w.temp_c)}C</td>
                <td>{w.precip_mm.toFixed(1)}mm</td>
                <td>{Math.round(w.wind_kmh)}km/h</td>
                <td>{Math.round(w.cloud_pct)}%</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <p class="card-empty">No viable weather windows in the forecast.</p>
      {/if}

      <a class="card-link" href={selected.url} target="_blank" rel="noopener">
        View on WalkHighlands &nearr;
      </a>
    </aside>
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

  /* Walk detail card: a hard-shadow paper card (neobrutalist), top-right and
     clear of the global app chrome. */
  .card {
    position: absolute;
    top: 16px;
    right: 16px;
    width: 320px;
    max-width: calc(100% - 32px);
    max-height: calc(100% - 32px);
    overflow-y: auto;
    padding: 18px;
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

  .card-stats {
    display: flex;
    gap: 14px;
    margin-bottom: 12px;
  }

  .card-stats > div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .card-stats dt {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .card-stats dd {
    font-family: var(--mono);
    font-size: 14px;
    font-weight: 700;
  }

  .card-summary {
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.45;
    color: var(--ink-2);
    margin-bottom: 14px;
  }

  .card-windows-title {
    margin-bottom: 8px;
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

  .card-link {
    display: inline-block;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-decoration-skip-ink: none;
    text-underline-offset: 2px;
    padding: 0 2px;
    transition: background 140ms ease;
  }

  .card-link:hover,
  .card-link:focus-visible {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
    text-decoration-color: var(--ink);
  }

  @media (max-width: 640px) {
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
