<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import {
    relativeMax,
    monthBars,
    liveWindows,
    nightKey,
  } from "$lib/public/stars/heat.js";

  // `sites` is the list to plot; clicking a dot opens the detail card. In LIVE
  // mode the rows carry per-night scores + upcoming clear-dark hours; in
  // HISTORICAL mode they carry clear-dark-hour counts (clear_dark_hours /
  // dark_hours / clear_rate) for the selected view plus the per-site `clear`
  // array (12 months, index 0 = January) the card graph draws from. The parent
  // projects both from the single all-months /history payload, so the card needs
  // no extra fetch (stars v2 metric).
  // `activeNights` is the set of selected night keys from the parent's night
  // filter (LIVE only): a marker is coloured by the best score it reaches across
  // those nights, and drops off the map when none of its hours fall on a
  // selected night. `nowMs` is a coarse clock signal so the card can drop hours
  // that have already elapsed between SSR loads. `mode` ("live"|"historical")
  // switches how each feature's heat + marker value is derived and which card
  // body renders. `heatVisible` toggles between the point markers and the
  // box-cell heatmap: when on, the circle markers hide and each site renders as
  // a coloured grid cell (ADR 009, mirroring /app/ships' fill-layer heatmap).
  // `darknessMode` ("astronomical"|"twilight"|"none") is the page-level live
  // darkness state. In "twilight" (the ~7-week midsummer window where Scotland
  // gets no true dark) every site's clear_dark_hours is zero, so the live
  // marker/cell value falls back to the wider clear_twilight_hours so the map
  // still has a meaningful field to colour. LIVE only; ignored in historical.
  let {
    sites = [],
    activeNights = new Set(),
    nowMs = Date.now(),
    mode = "live",
    darknessMode = "astronomical",
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

  // How many upcoming windows the detail card lists at once. best_hours ships
  // more than this (see _DISPLAY_HOURS server-side) so the elapsed-filtered list
  // still fills the card through a night as the earliest hours drop off.
  const CARD_HOURS = 8;

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

  // The selected site's upcoming windows for the card list: the same elapsed-
  // filtered best_hours the map field is derived from (so a coloured cell always
  // has card rows), capped at the handful the card shows at once. LIVE only;
  // historical rows carry no best_hours.
  let cardHours = $derived(liveWindows(selected, nowMs).slice(0, CARD_HOURS));

  // Group the live windows by London-local day so the list shows each day once
  // (a rowspan label) with its hours beneath, rather than repeating "Mon, Mon,
  // Mon...". best_hours can arrive ranked by cloud rather than time, so sort the
  // groups and the hours within each group chronologically here. The day key is
  // the real date (YYYY-MM-DD), so this Monday and next Monday stay distinct.
  let windowGroups = $derived.by(() => {
    const byDay = new Map();
    for (const h of cardHours) {
      const ms = Date.parse(h.time);
      const key = new Date(ms).toLocaleDateString("en-CA", {
        timeZone: "Europe/London",
      });
      if (!byDay.has(key)) {
        byDay.set(key, { key, day: fmtDay(h.time), ms, items: [] });
      }
      const g = byDay.get(key);
      g.items.push(h);
      if (ms < g.ms) g.ms = ms;
    }
    const groups = [...byDay.values()];
    groups.sort((a, b) => a.ms - b.ms);
    for (const g of groups) {
      g.items.sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
    }
    return groups;
  });

  // Honest title for the live windows table: "clear dark hours" only when every
  // shown hour is true dark (sun < -12). In the midsummer twilight fallback some
  // or all hours are twilight-only, so the wider "clear windows" reads true. An
  // older payload without per-hour `dark` flags (undefined) is treated as dark,
  // keeping the original label.
  let windowsAllDark = $derived(cardHours.every((h) => h.dark !== false));
  let windowsTitle = $derived(
    windowsAllDark ? "Upcoming clear dark hours" : "Upcoming clear windows",
  );

  let mapsLink = $derived(
    selected
      ? `https://www.google.com/maps/search/?api=1&query=${selected.lat},${selected.lon}`
      : null,
  );

  // Split the window timestamp into a weekday ("Sun") and a clock ("01:00") so
  // the table keeps two narrow columns instead of one wide combined cell.
  function fmtDay(iso) {
    return new Date(iso).toLocaleDateString("en-GB", {
      weekday: "short",
      timeZone: "Europe/London",
    });
  }

  function fmtClock(iso) {
    return new Date(iso).toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/London",
    });
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

  // Upcoming clear-window count this site reaches across the currently selected
  // nights, derived from the same best_hours array the card lists (so a coloured
  // cell always has card rows). In astronomical mode only true-dark hours count
  // toward the field; in the midsummer "twilight" mode the whole clear-twilight
  // superset counts so the field is not uniformly zero. Hours with no `dark`
  // flag (older payload) count as dark, preserving prior behaviour. With one or
  // more selected nights the site takes its best single night's count; a site
  // with no qualifying windows on the active nights returns null and drops off
  // the map (the night filter). LIVE only.
  function effectiveClearDark(site) {
    const darkOnly = darknessMode !== "twilight";
    const wins = liveWindows(site, nowMs).filter(
      (h) => !darkOnly || h.dark !== false,
    );
    if (!activeNights || activeNights.size === 0) {
      return wins.length || null;
    }
    const perNight = new Map();
    for (const h of wins) {
      const night = nightKey(h.time);
      if (!activeNights.has(night)) continue;
      perNight.set(night, (perNight.get(night) ?? 0) + 1);
    }
    let best = null;
    for (const c of perNight.values()) {
      if (best === null || c > best) best = c;
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

  // Recolour when the page flips between dark and twilight darkness modes: the
  // live field then derives from a different per-site count (dark vs twilight).
  $effect(() => {
    void darknessMode;
    if (map && layerReady && mode === "live") pushData();
  });

  // Recolour as the clock advances: the live field elapsed-filters best_hours,
  // so a tick can drop a site whose last window has just passed. Keeps the
  // markers in step with the card (which re-derives on nowMs), so a coloured
  // cell never outlives its windows. LIVE only.
  $effect(() => {
    void nowMs;
    if (map && layerReady && mode === "live") pushData();
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
      <!-- The section title is always present, so it anchors a header row with
           the close glyph on its right: no empty band, and the glyph needs no
           border of its own. -->
      <header class="card-head">
        <p class="card-title">
          {mode === "historical" ? "Clear dark hours by month" : windowsTitle}
        </p>
        <button
          class="card-close"
          onclick={closeCard}
          aria-label="Close site card">&times;</button
        >
      </header>

      {#if mode === "historical"}
        <!-- Historical body: the clear-dark-hour count for the selected view
             (sun below -12 deg AND under 10% cloud), its dark-hour denominator,
             the clear rate, and a 12-month breakdown so the seasonal shape
             reads at a glance (stars v2). No hourly windows. -->
        <dl class="card-metric">
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

        {@const bars = monthBars(selected.clear)}
        <!-- Inline SVG bar chart (no charting dep): one bar per month-of-year,
             height relative to the busiest month, the tallest bar(s) accented
             and value-labelled. viewBox does the scaling so the CSS width keeps
             it crisp. Mono labels + 2px ink strokes mirror the card chrome. The
             per-month data rides on the selected row (selected.clear) from the
             single all-months payload, so there is no per-card fetch. -->
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
            {@const h =
              bar.value > 0 ? Math.max(2, Math.round(bar.frac * 70)) : 0}
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
            <text x={x + bw / 2} y={100} class="chart-tick">{bar.short[0]}</text
            >
          {/each}
        </svg>
        <p class="card-empty">
          Hours with the sun below -12 deg and under 10% cloud, from the ERA5
          reanalysis seasonal baseline. Taller bars are the months this spot
          clears most.
        </p>
      {:else}
        {#if cardHours.length}
          <table class="card-windows">
            <thead>
              <tr>
                <th>Day</th>
                <th>Time</th>
                <th>Cloud</th>
              </tr>
            </thead>
            <tbody>
              {#each windowGroups as g (g.key)}
                {#each g.items as h, i (h.time)}
                  <tr class:day-start={i === 0}>
                    {#if i === 0}
                      <td class="win-day" rowspan={g.items.length}>{g.day}</td>
                    {/if}
                    <td>{fmtClock(h.time)}</td>
                    <td>{Math.round(h.cloud_area_fraction)}%</td>
                  </tr>
                {/each}
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="card-empty">No upcoming clear windows for this site.</p>
        {/if}
      {/if}

      <!-- Location footer: the coordinates themselves are the link out to maps
           (this is the card's primary action, hence the accent fill). -->
      <a class="card-loc" href={mapsLink} target="_blank" rel="noopener">
        <span class="card-loc-coord"
          >{fmtCoord(selected.lat, selected.lon)}<span
            class="card-loc-arrow"
            aria-hidden="true">&nearr;</span
          ></span
        >
      </a>
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
  }

  /* Header row: the section title (always present) anchors the left, the close
     glyph the right. A bare muted glyph, not a bordered box: it darkens on
     hover, keeps a 40px hit area, and uses negative margins to tuck into the
     corner without inflating the row height. */
  .card-head {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }

  .card-title {
    flex: 1 1 auto;
    margin: 0;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink);
  }

  .card-close {
    flex: 0 0 auto;
    margin: -8px -8px -8px 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    background: none;
    border: none;
    font-family: var(--mono);
    font-size: 26px;
    line-height: 1;
    cursor: pointer;
    color: var(--ink-3);
    transition: color 110ms ease;
  }

  .card-close:hover,
  .card-close:focus-visible {
    color: var(--ink);
  }

  /* Historical headline metrics: a plain inline row of label/value pairs, the
     numbers carrying the weight, no box. */
  .card-metric {
    display: flex;
    flex-wrap: wrap;
    gap: 12px 20px;
    margin-bottom: 14px;
  }

  .card-metric > div {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .card-metric dt {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .card-metric dd {
    margin: 0;
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 700;
  }

  /* Day / Time / Cloud kept simple: one rule under the header, generous row
     padding instead of a line on every row. */
  .card-windows {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 13px;
    margin-bottom: 6px;
  }

  .card-windows th {
    text-align: left;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    border-bottom: 2px solid var(--ink);
    padding: 0 8px 8px 0;
  }

  .card-windows td {
    padding: 9px 8px 9px 0;
    font-weight: 700;
  }

  /* Cloud reads as a right-aligned number column. */
  .card-windows th:last-child,
  .card-windows td:last-child {
    text-align: right;
    padding-right: 0;
  }

  /* Day label spans its group's hours (rowspan), sitting top-aligned against the
     first time. One light rule separates day groups so the list reads in blocks
     without a line on every row. */
  .card-windows .win-day {
    vertical-align: top;
    padding-right: 18px;
    font-weight: 700;
  }

  .card-windows tbody tr.day-start:not(:first-child) td {
    border-top: 1px solid var(--rule-2);
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

  /* Location footer: a full-bleed accent block (yellow + ink, high contrast and
     readable) that borrows the card's bottom + side edges and is set off by a
     single top rule. The whole block is the maps link and the card's primary
     action; it inverts to ink/paper on hover for press feedback. */
  .card-loc {
    display: flex;
    align-items: center;
    margin: 16px -18px -18px;
    padding: 16px 18px;
    background: var(--accent);
    border-top: 2px solid var(--ink);
    color: var(--ink);
    text-decoration: none;
    font-family: var(--mono);
    transition:
      background 110ms ease,
      color 110ms ease;
  }

  .card-loc:hover,
  .card-loc:focus-visible {
    background: var(--ink);
    color: var(--paper);
  }

  .card-loc-coord {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 15px;
    font-weight: 700;
  }

  .card-loc-arrow {
    font-size: 0.9em;
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
