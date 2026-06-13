<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";

  // `sites` is the curated dark-sky list to plot; clicking a dot opens the
  // detail card. `activeNights` is the set of selected night keys (evening
  // dates) from the parent's night filter: a marker is coloured by the best
  // score it reaches across those nights, and drops off the map when none of
  // its hours fall on a selected night. `nowMs` is a coarse clock signal from
  // the parent so the card can drop hours that have already elapsed between
  // SSR loads.
  let { sites = [], activeNights = new Set(), nowMs = Date.now() } = $props();

  // OpenFreeMap needs no API key (same hosted liberty style as /app/ships and
  // /app/hikes, so the maps read as siblings). The liberty style ships light and
  // we keep it light: these are daytime drive-planning maps, the dark skies are
  // the subject, not the chrome.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  // Score buckets: muted (poor) -> blue (fair) -> violet (prime). Hardcoded in
  // JS (a data-viz ramp, not a design-system surface), tuned to read on the
  // light basemap; the prime stop is the funky violet that also marks Stars in
  // the nav. Mirrors how HikesMap keeps its EFFORT_RAMP stops in JS and renders
  // the legend swatches via inline style attributes.
  //
  // Boundaries follow the ADR 007 continuous quality Q = D x C x W: an ideal
  // winter night reaches ~90+, while summer's best (sun never far below the
  // horizon) tops out around 30-50, so the buckets break at 35 / 65.
  const SCORE_BUCKETS = [
    { key: "low", label: "< 35", color: "#94a3b8" }, // muted slate
    { key: "mid", label: "35-65", color: "#3b82f6" }, // blue
    { key: "high", label: "65+", color: "#7c3aed" }, // violet (prime)
  ];

  const SOURCE_ID = "sites";
  const LAYER_ID = "sites";

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let layerReady = false;
  let didFit = false;

  // site id -> site row, so a marker click can recover the full record.
  const index = new Map();

  let selected = $state(null); // selected site row, or null

  // The selected site's hours, dropping any that have already elapsed (the hour
  // is over once nowMs passes its end). The endpoint already prunes past hours
  // server-side; this just keeps a long-open page honest between refreshes.
  let cardHours = $derived(
    (selected?.best_hours ?? []).filter((h) => {
      const t = Date.parse(h.time);
      return Number.isNaN(t) ? true : t + 3_600_000 > nowMs;
    }),
  );

  let mapsLink = $derived(
    selected
      ? `https://www.google.com/maps/search/?api=1&query=${selected.lat},${selected.lon}`
      : null,
  );

  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-GB", {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/London",
    });
  }

  function fmtSymbol(symbol) {
    // met.no symbol codes like "clearsky_night" -> "clearsky night".
    return symbol ? symbol.replace(/_/g, " ") : "n/a";
  }

  function fmtCoord(lat, lon) {
    const ns = lat >= 0 ? "N" : "S";
    const ew = lon >= 0 ? "E" : "W";
    return `${Math.abs(lat).toFixed(3)}°${ns}, ${Math.abs(lon).toFixed(3)}°${ew}`;
  }

  function selectSite(site) {
    selected = site;
  }

  function closeCard() {
    selected = null;
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

  // Marker stroke comes from the design tokens (ink), a dark ring that lifts
  // every dot off the light basemap regardless of its score colour.
  function palette() {
    const s = getComputedStyle(document.documentElement);
    return {
      ink: s.getPropertyValue("--ink").trim(),
    };
  }

  // Best score this site reaches across the currently selected nights. Falls
  // back to best_score (always present) when the payload carries no per-night
  // data, so an older build or a CDN-cached pre-feature response never blanks
  // the map; with night data and "All" selected this still equals best_score.
  // A single selected night returns null for sites with no window then, so they
  // drop off the map (the actual filter).
  function effectiveScore(site) {
    const ns = site.night_scores;
    if (!ns || !activeNights || activeNights.size === 0) {
      return site.best_score ?? null;
    }
    let best = null;
    for (const night of activeNights) {
      const s = ns[night];
      if (s != null && (best === null || s > best)) best = s;
    }
    return best;
  }

  function buildFeatures() {
    const features = [];
    for (const site of sites) {
      if (!validLatLon(site.lat, site.lon)) continue;
      const score = effectiveScore(site);
      if (score === null) continue;
      features.push({
        type: "Feature",
        id: site.id,
        geometry: { type: "Point", coordinates: [site.lon, site.lat] },
        properties: { id: site.id, score },
      });
    }
    return { type: "FeatureCollection", features };
  }

  function pushData() {
    const src = map?.getSource(SOURCE_ID);
    if (src) src.setData(buildFeatures());
  }

  function fitToSites() {
    const pts = sites.filter((s) => validLatLon(s.lat, s.lon));
    if (didFit || !pts.length) return;
    const b = new maplibregl.LngLatBounds();
    for (const s of pts) b.extend([s.lon, s.lat]);
    map.fitBounds(b, { padding: 64, maxZoom: 8, duration: 0 });
    didFit = true;
  }

  function syncSites() {
    if (!map || !layerReady) return;
    index.clear();
    for (const s of sites) index.set(s.id, s);
    pushData();
    fitToSites();
    // If the open card's site fell out of the set, close it.
    if (selected && !index.has(selected.id)) closeCard();
  }

  // Repaint whenever the site set changes.
  $effect(() => {
    void sites;
    if (map && layerReady) syncSites();
  });

  // Recolour/refilter the markers when the night selection changes. setData
  // alone (no refit) keeps the viewport put while the dots restyle.
  $effect(() => {
    void activeNights;
    if (map && layerReady) pushData();
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
        // Scotland: every curated dark-sky site is up north, so frame there
        // until the first fitBounds lands.
        center: [-4.2, 57.0],
        zoom: 6,
        attributionControl: { compact: true },
      });
      map.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );

      // Collapse the compact attribution on every update so it never paints as
      // a full-width credit bar; leave it alone once the user taps it open.
      // Same handling as ShipsMap / HikesMap.
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
            // Fill by best score: muted slate -> blue -> violet, stepped at
            // the 35 / 65 bucket boundaries (ADR 007 continuous quality). Ink
            // stroke keeps every dot legible on the light basemap; better sites
            // read a touch larger.
            "circle-color": [
              "step",
              ["get", "score"],
              SCORE_BUCKETS[0].color,
              35,
              SCORE_BUCKETS[1].color,
              65,
              SCORE_BUCKETS[2].color,
            ],
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["get", "score"],
              0,
              5,
              100,
              11,
            ],
            "circle-stroke-width": 1.5,
            "circle-stroke-color": pal.ink,
          },
        });
        layerReady = true;

        map.on("click", LAYER_ID, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          const site = index.get(f.properties.id);
          if (site) selectSite(site);
        });
        map.on("mouseenter", LAYER_ID, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", LAYER_ID, () => {
          map.getCanvas().style.cursor = "";
        });

        syncSites();
      });

      cleanup = () => {
        index.clear();
        map?.remove();
        map = null;
        layerReady = false;
        didFit = false;
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
    <aside class="card">
      <button class="card-close" onclick={closeCard} aria-label="Close site card"
        >&times;</button
      >
      <h2 class="card-name">{selected.name}</h2>
      <dl class="card-stats">
        <div>
          <dt>Coordinates</dt>
          <dd>{fmtCoord(selected.lat, selected.lon)}</dd>
        </div>
        <div><dt>Altitude</dt><dd>{selected.altitude_m} m</dd></div>
        <div><dt>LP zone</dt><dd>{selected.lp_zone}</dd></div>
      </dl>

      {#if cardHours.length}
        <p class="eyebrow card-windows-title">Best upcoming hours</p>
        <table class="card-windows">
          <thead>
            <tr>
              <th>Time</th>
              <th>Score</th>
              <th>Cloud</th>
              <th>Temp</th>
              <th>Dew</th>
              <th>Sky</th>
            </tr>
          </thead>
          <tbody>
            {#each cardHours as h (h.time)}
              <tr>
                <td>{fmtTime(h.time)}</td>
                <td>{Math.round(h.score)}</td>
                <td>{Math.round(h.cloud_area_fraction)}%</td>
                <td>{Math.round(h.air_temperature)}C</td>
                <td>{h.dew_spread?.toFixed(1)}</td>
                <td>{fmtSymbol(h.symbol)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <p class="card-empty">No upcoming viewing hours for this site.</p>
      {/if}

      <a class="card-link" href={mapsLink} target="_blank" rel="noopener"
        >Open in maps<span class="card-link-arrow" aria-hidden="true">&nearr;</span></a
      >
    </aside>
  {/if}

  <!-- Score legend: the colour buckets that tint the dots. -->
  <div class="legend">
    <span class="legend-title">Best score</span>
    <ul class="legend-list">
      {#each SCORE_BUCKETS as b (b.key)}
        <li>
          <span class="legend-sw" style="background: {b.color}"></span>{b.label}
        </li>
      {/each}
    </ul>
  </div>
</div>

<style>
  .map-wrap {
    position: absolute;
    inset: 0;
    overflow: hidden;
    /* Light base behind the map while tiles stream in, so the load flash
       matches the light liberty basemap rather than flashing dark. */
    background: var(--paper);
  }

  .map {
    position: absolute;
    inset: 0;
  }

  /* Inset the bottom-right control stack past device safe areas (same reasons
     as ShipsMap / HikesMap: home indicator + landscape notch). */
  .map :global(.maplibregl-ctrl-bottom-right) {
    bottom: env(safe-area-inset-bottom, 0);
    right: env(safe-area-inset-right, 0);
  }

  /* Each zoom button is its own square that lifts off (neobrutalist), matching
     the ships + hikes maps and the homepage buttons. */
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

  /* Site detail card: a flat sharp-bordered paper overlay, top-right and clear
     of the global app chrome (matches the hikes + ships overlays). */
  .card {
    position: absolute;
    top: 16px;
    right: 16px;
    width: 340px;
    max-width: calc(100% - 32px);
    max-height: calc(100% - 32px);
    overflow-y: auto;
    padding: 18px;
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard);
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
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 10px 12px;
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
    font-size: 14px;
    font-weight: 700;
  }

  /* Highlighted header bar so the table reads as the answer, not a faint grey
     eyebrow (overrides .eyebrow's muted colour). */
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
    text-underline-offset: 3px;
    transition: background 140ms ease;
  }

  .card-link:hover,
  .card-link:focus-visible {
    background: linear-gradient(transparent 62%, var(--accent) 62%);
    text-decoration-color: var(--ink);
  }

  .card-link-arrow {
    font-size: 0.85em;
    margin-left: 2px;
  }

  /* Score legend, bottom-left, clear of the bottom-right zoom + attribution. */
  .legend {
    position: absolute;
    left: 16px;
    bottom: 16px;
    z-index: 4;
    display: flex;
    flex-direction: column;
    gap: 6px;
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

  .legend-list {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--ink);
  }

  .legend-list li {
    display: flex;
    align-items: center;
    gap: 7px;
  }

  .legend-sw {
    width: 13px;
    height: 13px;
    border: 1.5px solid var(--ink);
    flex: none;
  }

  @media (max-width: 640px) {
    /* On phones the card is a bottom sheet, so the legend would collide with
       it; drop the legend there (the colours stay self-evident). */
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
      max-height: 72vh;
      border-width: 2px 0 0;
      box-shadow: none;
      padding: 20px 18px calc(20px + env(safe-area-inset-bottom));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .map :global(.maplibregl-ctrl-group button) {
      transition: none;
    }
  }
</style>
