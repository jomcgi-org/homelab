<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import { relativeMax, monthBars } from "$lib/public/stars/heat.js";

  // `sites` is the list to plot; clicking a dot opens the detail card. In LIVE
  // mode the rows carry per-night scores + upcoming clear-dark hours; in
  // HISTORICAL mode they carry banked clear-dark-hour counts (clear_dark_hours /
  // dark_hours / clear_rate) plus a per-month {1..12} breakdown for the card
  // graph (stars v2 metric).
  // `activeNights` is the set of selected night keys from the parent's night
  // filter (LIVE only): a marker is coloured by the best score it reaches across
  // those nights, and drops off the map when none of its hours fall on a
  // selected night. `nowMs` is a coarse clock signal so the card can drop hours
  // that have already elapsed between SSR loads. `mode` ("live"|"historical")
  // switches how each feature's heat + marker value is derived and which card
  // body renders. `heatVisible` toggles between the point markers and the
  // box-cell heatmap: when on, the circle markers hide and each site renders as
  // a coloured grid cell (ADR 009, mirroring /app/ships' fill-layer heatmap).
  let {
    sites = [],
    activeNights = new Set(),
    nowMs = Date.now(),
    mode = "live",
    heatVisible = false,
  } = $props();

  // OpenFreeMap needs no API key (same hosted liberty style as /app/ships and
  // /app/hikes, so the maps read as siblings). The liberty style ships light and
  // we keep it light: these are daytime drive-planning maps, the dark skies are
  // the subject, not the chrome.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  // Marker buckets: muted (poor) -> blue (fair) -> violet (prime). Hardcoded in
  // JS (a data-viz ramp, not a design-system surface), tuned to read on the
  // light basemap; the prime stop is the funky violet that also marks Stars in
  // the nav. Mirrors how HikesMap keeps its EFFORT_RAMP stops in JS.
  //
  // The marker `score` is always a 0..100 RELATIVE percentile of clear-dark hours
  // (heat / max-heat) in both modes, so the 35 / 65 breaks read as low / medium /
  // high thirds of whatever is in view. Colours only (the legend labels these
  // Low/Medium/High); muted slate -> blue -> violet (prime), tuned for the light
  // basemap, the violet matching the Stars nav accent.
  const SCORE_BUCKETS = [
    { key: "low", label: "Low", color: "#94a3b8" }, // muted slate
    { key: "mid", label: "Medium", color: "#3b82f6" }, // blue
    { key: "high", label: "High", color: "#7c3aed" }, // violet (prime)
  ];

  // Box-cell heatmap ramp (cool -> warm), keyed on each cell's `score` (0..100:
  // absolute live quality, or a relative percentile of clear-dark hours in
  // historical). Like
  // ShipsMap's HEAT_STOPS these are JS map-paint colours, not design tokens: a
  // saturated plasma scale that stays vivid on the light basemap. Smooth
  // `interpolate` (not stepped) so the field reads as a heatmap; the three named
  // stops below feed the legend so it stays truthful to the fill.
  const CELL_LOW = "#3b82f6"; // blue (low)
  const CELL_MID = "#d61f9c"; // magenta (medium)
  const CELL_HIGH = "#ff2a1f"; // hot red (best)
  const CELL_FILL_COLOR = [
    "interpolate",
    ["linear"],
    ["get", "score"],
    0,
    CELL_LOW,
    35,
    "#7c3aed", // violet
    65,
    CELL_MID,
    85,
    "#ff6a00", // orange
    100,
    CELL_HIGH,
  ];
  // Fill opacity: high enough to read as solid blocks over the light basemap,
  // low enough that coastlines/place labels still ghost through at the edges
  // (ShipsMap uses 0.9 over water; stars cells sit on land so go a touch lower).
  const CELL_FILL_OPACITY = 0.72;

  const SOURCE_ID = "sites"; // point features -> circle markers
  const LAYER_ID = "sites";
  const CELL_SOURCE = "sites-cells"; // square polygons -> fill heatmap
  const CELL_LAYER = "sites-cells";

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let layerReady = false;
  let didFit = false;

  // The relative-normalization ceiling for the current feature set, restamped on
  // every plottable(). Drives the historical 0..100 percentile that both the
  // markers and the box-cell fill colour by, so the field re-normalizes as
  // mode/night/month changes. Defaults to the live score ceiling.
  let currentMaxHeat = 100;

  // site id -> site row, so a marker click can recover the full record.
  const index = new Map();

  let selected = $state(null); // selected site row, or null

  // The legend follows what is actually drawn. The field is always a relative
  // clear-dark-hour percentile, so it reads low/medium/high either way: the cell
  // ramp colours when the box heatmap is on (always HISTORICAL, optional LIVE),
  // the marker bucket colours when markers show (LIVE, heat off).
  let legendTitle = $derived("Clear dark hours");
  let legendItems = $derived(
    heatVisible
      ? [
          { key: "low", label: "Low", color: CELL_LOW },
          { key: "mid", label: "Medium", color: CELL_MID },
          { key: "high", label: "High", color: CELL_HIGH },
        ]
      : [
          { key: "low", label: "Low", color: SCORE_BUCKETS[0].color },
          { key: "mid", label: "Medium", color: SCORE_BUCKETS[1].color },
          { key: "high", label: "High", color: SCORE_BUCKETS[2].color },
        ],
  );

  // The selected site's hours, dropping any that have already elapsed (the hour
  // is over once nowMs passes its end). LIVE only; historical rows carry no
  // best_hours. The endpoint already prunes past hours server-side; this just
  // keeps a long-open page honest between refreshes.
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

  // clear_rate arrives as a 0..1 fraction (clear_dark_hours / dark_hours);
  // render it as a whole-number percentage, guarding null/NaN to 0%.
  function fmtPct(rate) {
    const r = typeof rate === "number" && Number.isFinite(rate) ? rate : 0;
    return `${Math.round(r * 100)}%`;
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

  // Upcoming clear-dark-hour count this site reaches across the currently
  // selected nights. Falls back to the site-level clear_dark_hours when the
  // payload carries no per-night map (older build / CDN-cached response), so the
  // map never blanks. With a single selected night, sites with no clear-dark
  // hours that night return null and drop off the map (the night filter). LIVE
  // only. night_clear_dark only holds nights with >= 1 clear-dark hour, so a
  // missing key is correctly treated as zero (dropped under a single night).
  function effectiveClearDark(site) {
    const nd = site.night_clear_dark;
    if (!nd || !activeNights || activeNights.size === 0) {
      return site.clear_dark_hours ?? null;
    }
    let best = null;
    for (const night of activeNights) {
      const c = nd[night];
      if (c != null && (best === null || c > best)) best = c;
    }
    return best;
  }

  // The raw heat value for a site in the current mode: LIVE = the count of
  // upcoming clear-dark hours across the selected nights, HISTORICAL = the banked
  // clear-dark-hour count for the selected view (a month bucket or the all-year
  // sum). null drops the site (LIVE: no clear-dark hours on the selected night;
  // HISTORICAL: missing/zero count).
  function rawHeat(site) {
    if (mode === "historical") {
      const c = site.clear_dark_hours;
      return typeof c === "number" && c > 0 ? c : null;
    }
    return effectiveClearDark(site);
  }

  // The plottable sites with their derived marker/cell `score`, computed once so
  // the point and cell feature collections share one normalization. Both modes
  // measure clear-dark hours (LIVE = upcoming count, HISTORICAL = banked count),
  // so both map onto a 0..100 RELATIVE percentile (heat / max-heat): the 35 / 65
  // ramp breaks read as low / medium / high thirds of whatever is in view, and
  // the field rescales as the night/month/all-year view changes. Sites with no
  // clear-dark hours in the current mode/night drop out here.
  function plottable() {
    const raw = [];
    for (const site of sites) {
      if (!validLatLon(site.lat, site.lon)) continue;
      const heat = rawHeat(site);
      if (heat === null) continue;
      raw.push({ site, heat });
    }
    // Relative ceiling: the richest site reaches full scale regardless of the
    // absolute count (a short forecast horizon vs the 5-year banked history),
    // restamped here so the score rescales to whatever is in view.
    currentMaxHeat = relativeMax(raw.map((r) => r.heat));
    return raw.map(({ site, heat }) => ({
      site,
      score: (100 * heat) / currentMaxHeat,
    }));
  }

  function pointFC(rows) {
    return {
      type: "FeatureCollection",
      features: rows.map(({ site, score }) => ({
        type: "Feature",
        id: site.id,
        geometry: { type: "Point", coordinates: [site.lon, site.lat] },
        properties: { id: site.id, score },
      })),
    };
  }

  // The grid step in degrees, derived from the plotted sites: the smallest
  // positive gap between adjacent unique lats/lons. The grid is a regular ~4km
  // mesh, so every gap is an integer multiple of the base step and the minimum
  // recovers it. Falls back to the known ~4km step if a set is too small to
  // measure (e.g. a single site in view).
  function deriveStep(rows) {
    const uniq = (vals) =>
      [...new Set(vals.map((v) => Math.round(v * 1e6) / 1e6))].sort(
        (a, b) => a - b,
      );
    const minGap = (xs, fallback) => {
      let m = Infinity;
      for (let i = 1; i < xs.length; i++) {
        const d = xs[i] - xs[i - 1];
        if (d > 1e-6 && d < m) m = d;
      }
      return Number.isFinite(m) ? m : fallback;
    };
    const lats = uniq(rows.map((r) => r.site.lat));
    const lons = uniq(rows.map((r) => r.site.lon));
    return { latStep: minGap(lats, 0.036), lonStep: minGap(lons, 0.067) };
  }

  // Each site becomes a square cell centred on its lat/lon, sized to the grid
  // step, so the historical layer reads as discrete blocks (like /app/ships)
  // rather than a blurry bloom. Same `score` as the point features, so the fill
  // ramp and the (hidden) markers always agree.
  function cellFC(rows) {
    const { latStep, lonStep } = deriveStep(rows);
    const hLat = latStep / 2;
    const hLon = lonStep / 2;
    return {
      type: "FeatureCollection",
      features: rows.map(({ site, score }) => {
        const x = site.lon;
        const y = site.lat;
        return {
          type: "Feature",
          id: site.id,
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [x - hLon, y - hLat],
                [x + hLon, y - hLat],
                [x + hLon, y + hLat],
                [x - hLon, y + hLat],
                [x - hLon, y - hLat],
              ],
            ],
          },
          properties: { id: site.id, score },
        };
      }),
    };
  }

  function pushData() {
    if (!map) return;
    const rows = plottable();
    map.getSource(SOURCE_ID)?.setData(pointFC(rows));
    map.getSource(CELL_SOURCE)?.setData(cellFC(rows));
  }

  // The box heatmap and the point markers are mutually exclusive: when heat is
  // on the cells show and the markers hide, and vice versa. Both stay clickable
  // when visible (a hidden layer receives no events), so the detail card works
  // either way.
  function applyHeatVisibility() {
    if (!map || !layerReady) return;
    map.setLayoutProperty(
      CELL_LAYER,
      "visibility",
      heatVisible ? "visible" : "none",
    );
    map.setLayoutProperty(
      LAYER_ID,
      "visibility",
      heatVisible ? "none" : "visible",
    );
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

  // Mode flip (live <-> historical) swaps what each feature means, so rebuild the
  // data and drop any open card (its row shape belongs to the other mode).
  $effect(() => {
    void mode;
    if (map && layerReady) {
      closeCard();
      pushData();
    }
  });

  // Swap markers <-> box-cell heatmap when the parent toggles heat on/off.
  $effect(() => {
    void heatVisible;
    if (map && layerReady) applyHeatVisibility();
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
        map.addSource(CELL_SOURCE, { type: "geojson", data: emptyFC() });

        // Box-cell heatmap first, so it sits BENEATH the markers. One square per
        // site, filled by the cell `score` via the plasma ramp; visible only when
        // the parent toggles heat on (always in historical), where the markers
        // hide. Transparent outline so adjacent same-score cells merge into a
        // field rather than a grid of boxes.
        map.addLayer({
          id: CELL_LAYER,
          type: "fill",
          source: CELL_SOURCE,
          layout: { visibility: heatVisible ? "visible" : "none" },
          paint: {
            "fill-color": CELL_FILL_COLOR,
            "fill-opacity": CELL_FILL_OPACITY,
            "fill-outline-color": "rgba(0, 0, 0, 0)",
          },
        });

        map.addLayer({
          id: LAYER_ID,
          type: "circle",
          source: SOURCE_ID,
          // Markers hide while the box heatmap is on (and vice versa).
          layout: { visibility: heatVisible ? "none" : "visible" },
          paint: {
            // Fill by the marker `score` value (absolute live quality, or a
            // 0..100 relative percentile in historical), stepped at the 35 / 65
            // bucket boundaries. Ink stroke keeps every dot legible on the light
            // basemap; better sites read a touch larger.
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

        // Click opens the detail card from either representation; only the
        // visible layer fires, so the two handlers never both run for one click.
        const openFromFeature = (e) => {
          const f = e.features?.[0];
          if (!f) return;
          const site = index.get(f.properties.id);
          if (site) selectSite(site);
        };
        const cursorPointer = () => (map.getCanvas().style.cursor = "pointer");
        const cursorReset = () => (map.getCanvas().style.cursor = "");
        for (const id of [LAYER_ID, CELL_LAYER]) {
          map.on("click", id, openFromFeature);
          map.on("mouseenter", id, cursorPointer);
          map.on("mouseleave", id, cursorReset);
        }

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

      {#if mode === "historical"}
        <!-- Historical body: the clear-dark-hour count for the selected view
             (sun below -12 deg AND under 10% cloud), its dark-hour denominator,
             the clear rate, and a 12-month breakdown so the seasonal shape
             reads at a glance (stars v2). No hourly windows. -->
        <dl class="card-stats">
          <div>
            <dt>Coordinates</dt>
            <dd>{fmtCoord(selected.lat, selected.lon)}</dd>
          </div>
          <div>
            <dt>Clear dark hours</dt>
            <dd>{selected.clear_dark_hours ?? 0}</dd>
          </div>
          <div>
            <dt>Dark hours</dt>
            <dd>{selected.dark_hours ?? 0}</dd>
          </div>
          <div>
            <dt>Clear rate</dt>
            <dd>{fmtPct(selected.clear_rate)}</dd>
          </div>
        </dl>

        {@const bars = monthBars(selected.months)}
        <p class="eyebrow card-windows-title">Clear dark hours by month</p>
        <!-- Inline SVG bar chart (no charting dep): one bar per month-of-year,
             height relative to the busiest month, the tallest bar(s) accented
             and value-labelled. viewBox does the scaling so the CSS width keeps
             it crisp. Mono labels + 2px ink strokes mirror the card chrome. -->
        <svg
          class="month-chart"
          viewBox="0 0 264 112"
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="Clear dark hours for each month of the year"
        >
          <line x1="6" y1="86" x2="258" y2="86" class="chart-axis" />
          {#each bars as bar (bar.month)}
            {@const bw = 16}
            {@const x = 8 + (bar.month - 1) * 20.8}
            {@const h = bar.value > 0 ? Math.max(2, Math.round(bar.frac * 70)) : 0}
            {#if h > 0}
              <rect
                {x}
                y={86 - h}
                width={bw}
                height={h}
                class="chart-bar"
                class:is-max={bar.isMax}
              />
            {/if}
            {#if bar.isMax && bar.value > 0}
              <text x={x + bw / 2} y={86 - h - 4} class="chart-val"
                >{bar.value}</text
              >
            {/if}
            <text x={x + bw / 2} y={100} class="chart-tick">{bar.short[0]}</text>
          {/each}
        </svg>
        <p class="card-empty">
          Hours with the sun below -12 deg and under 10% cloud, banked as forecast
          hours elapse and seeded from the ERA5 seasonal baseline. Taller bars are
          the months this spot clears most.
        </p>
      {:else}
        <dl class="card-stats">
          <div>
            <dt>Coordinates</dt>
            <dd>{fmtCoord(selected.lat, selected.lon)}</dd>
          </div>
          <div><dt>Altitude</dt><dd>{selected.altitude_m} m</dd></div>
          <div><dt>LP zone</dt><dd>{selected.lp_zone}</dd></div>
        </dl>

        {#if cardHours.length}
          <p class="eyebrow card-windows-title">Upcoming clear dark hours</p>
          <table class="card-windows">
            <thead>
              <tr>
                <th>Time</th>
                <th>Cloud</th>
                <th>Temp</th>
                <th>Sky</th>
              </tr>
            </thead>
            <tbody>
              {#each cardHours as h (h.time)}
                <tr>
                  <td>{fmtTime(h.time)}</td>
                  <td>{Math.round(h.cloud_area_fraction)}%</td>
                  <td>{Math.round(h.air_temperature)}C</td>
                  <td>{fmtSymbol(h.symbol)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="card-empty">No upcoming clear dark hours for this site.</p>
        {/if}
      {/if}

      <a class="card-link" href={mapsLink} target="_blank" rel="noopener"
        >Open in maps<span class="card-link-arrow" aria-hidden="true">&nearr;</span></a
      >
    </aside>
  {/if}

  <!-- Marker legend: the colour buckets that tint the dots (mode-aware). -->
  <div class="legend">
    <span class="legend-title">{legendTitle}</span>
    <ul class="legend-list">
      {#each legendItems as b (b.key)}
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

  /* 12-month clear-dark-hours bar chart: a flat SVG block sitting in the card,
     bars in the blue data colour with a 2px ink edge, the busiest month(s)
     flipped to accent so the seasonal peak pops. */
  .month-chart {
    display: block;
    width: 100%;
    height: auto;
    margin-bottom: 12px;
  }

  .month-chart .chart-axis {
    stroke: var(--ink);
    stroke-width: 2;
  }

  .month-chart .chart-bar {
    fill: var(--blue);
    stroke: var(--ink);
    stroke-width: 2;
  }

  .month-chart .chart-bar.is-max {
    fill: var(--accent);
  }

  .month-chart .chart-val {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    fill: var(--ink);
    text-anchor: middle;
  }

  .month-chart .chart-tick {
    font-family: var(--mono);
    font-size: 9px;
    fill: var(--ink-3);
    text-anchor: middle;
  }

  .card-empty {
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.5;
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

  /* Marker legend, bottom-left, clear of the bottom-right zoom + attribution. */
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
