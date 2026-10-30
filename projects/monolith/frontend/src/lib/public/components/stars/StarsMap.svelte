<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
  import Button from "@homelab/design-system/components/Button.svelte";
  import {
    readMapPalette,
    applyMapPalette,
    cellColors,
    markerColors,
  } from "$lib/public/stars/mapTheme.js";
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

  // Keep the hosted vector style and its geography. Paint follows the sheet
  // in place after load and on every explicit or system scheme change.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
  const SCORE_BUCKETS = [
    { key: "low", label: "Low", color: "var(--stars-ink-2)" },
    { key: "mid", label: "Medium", color: "var(--stars-low)" },
    { key: "high", label: "High", color: "var(--stars-quarter)" },
  ];
  const CELL_LOW = "var(--stars-low)";
  const CELL_MID = "var(--stars-mid)";
  const CELL_HIGH = "var(--stars-high)";
  // Retain the relative cool-to-warm data scale and translucent geography.
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
  // Keep prepared GeoJSON by input array and visual state. Mode switches should
  // swap already-built features, not synchronously walk every site again.
  const visualCache = new WeakMap();

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

  function visualData(cacheKey) {
    let byState = visualCache.get(sites);
    if (!byState) {
      byState = new Map();
      visualCache.set(sites, byState);
    }
    const cached = byState.get(cacheKey);
    if (cached) return cached;
    const rows = plottable();
    const prepared = { points: pointFC(rows), cells: cellFC(rows) };
    byState.set(cacheKey, prepared);
    return prepared;
  }

  function pushData() {
    if (!map) return;
    const nightKeyValue = [...(activeNights ?? [])].sort().join(",");
    const hourKey = Math.floor(nowMs / 3_600_000);
    const cacheKey = `${mode}|${darknessMode}|${hourKey}|${nightKeyValue}`;
    const prepared = visualData(cacheKey);
    map.getSource(SOURCE_ID)?.setData(prepared.points);
    map.getSource(CELL_SOURCE)?.setData(prepared.cells);
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
      maplibregl = await import("maplibre-gl");
      if (destroyed) return;
      // Vite must bundle the worker's shared imports too. The package-relative
      // default URL otherwise points at a missing asset in a production build.
      maplibregl.setWorkerUrl(workerUrl);

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
      const trackAttribution = (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      };
      mapContainer.addEventListener("click", trackAttribution);
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

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

      // MapLibre supplies keyboard pan and zoom. Enter selects the site under
      // the map centre; Escape closes its details without moving focus.
      const canvas = map.getCanvas();
      canvas.setAttribute("aria-label", "Dark-sky sites");
      canvas.setAttribute(
        "aria-description",
        "Arrow keys pan, plus and minus zoom, Enter selects a site at the map centre, Escape closes its details.",
      );
      const keyboard = (event) => {
        if (event.key === "Escape") closeCard();
        if (event.key !== "Enter" || !layerReady) return;
        event.preventDefault();
        const point = map.project(map.getCenter());
        const feature = map.queryRenderedFeatures(point, {
          layers: [heatVisible ? CELL_LAYER : LAYER_ID],
        })[0];
        const site = feature && index.get(feature.properties.id);
        if (site) selectSite(site);
      };
      canvas.addEventListener("keydown", keyboard);

      map.on("load", () => {
        const pal = readMapPalette(mapContainer);

        map.addSource(SOURCE_ID, { type: "geojson", data: emptyFC() });
        map.addSource(CELL_SOURCE, { type: "geojson", data: emptyFC() });

        // Box-cell heatmap first, so it sits BENEATH the markers. One square per
        // site, filled by the cell `score` via the sheet ramp; visible only when
        // the parent toggles heat on (always in historical), where the markers
        // hide. Transparent outline so adjacent same-score cells merge into a
        // field rather than a grid of boxes.
        map.addLayer({
          id: CELL_LAYER,
          type: "fill",
          source: CELL_SOURCE,
          layout: { visibility: heatVisible ? "visible" : "none" },
          paint: {
            "fill-color": cellColors(pal),
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
            // Relative clear-dark-hour percentile in both views, with the
            // existing 35 / 65 breaks. Better sites read a touch larger.
            "circle-color": markerColors(pal),
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["get", "score"],
              0,
              5,
              100,
              11,
            ],
            "circle-stroke-width": 1,
            "circle-stroke-color": pal.ink,
          },
        });
        layerReady = true;
        restyle();

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
        scheme.disconnect();
        media.removeEventListener("change", restyle);
        resize.disconnect();
        canvas.removeEventListener("keydown", keyboard);
        mapContainer.removeEventListener("click", trackAttribution);
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
    <aside
      class="card edition"
      aria-label={mode === "historical"
        ? "Clear dark hours by month"
        : windowsTitle}
    >
      <!-- The section title is always present, so it anchors a header row with
           the close glyph on its right: no empty band, and the glyph needs no
           border of its own. -->
      <header class="card-head ed-head">
        <p class="card-title">
          {mode === "historical" ? "Clear dark hours by month" : windowsTitle}
        </p>
        <Button
          class="card-close"
          variant="quiet"
          onclick={closeCard}
          aria-label="Close site card">&times;</Button
        >
      </header>

      {#if mode === "historical"}
        <!-- Historical body: the clear-dark-hour count for the selected view
             (sun below -12 deg AND under 10% cloud), its dark-hour denominator,
             the clear rate, and a 12-month breakdown so the seasonal shape
             reads at a glance (stars v2). No hourly windows. -->
        <dl class="card-metric spec">
          <div class="row">
            <dt>Clear dark hours</dt>
            <dd>{selected.clear_dark_hours ?? 0}</dd>
          </div>
          <div class="row">
            <dt>Dark hours</dt>
            <dd>{selected.dark_hours ?? 0}</dd>
          </div>
          <div class="row">
            <dt>Clear rate</dt>
            <dd>{fmtPct(selected.clear_rate)}</dd>
          </div>
        </dl>

        {@const bars = monthBars(selected.clear)}
        <!-- Inline SVG bar chart (no charting dep): one bar per month-of-year,
             height relative to the busiest month, the tallest bar(s) accented
             and value-labelled. viewBox does the scaling so the CSS width keeps
             it crisp. Mono labels and 1px rules follow the sheet. The
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

      <!-- Location footer: the coordinates themselves link out to maps. -->
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
    /* Data ramps retain low/cool, medium, high/warm meaning in both schemes.
       Geography uses neutral sheet roles so it cannot compete with the data. */
    --stars-sheet: var(--sheet);
    --stars-ink: var(--ink);
    --stars-ink-2: var(--ink-2);
    --stars-stroke: var(--stroke);
    --stars-band: var(--band);
    --stars-water: color-mix(in srgb, var(--tone-cache) 10%, var(--sheet));
    --stars-low: var(--tone-gpu);
    --stars-quarter: color-mix(in srgb, var(--tone-gpu), var(--tone-hot));
    --stars-mid: var(--tone-hot);
    --stars-upper: color-mix(in srgb, var(--tone-hot), var(--tone-disk));
    --stars-high: var(--tone-disk);
    position: relative;
    background: var(--sheet);
    min-width: 0;
  }

  .map {
    height: clamp(32rem, calc(100dvh - 7rem), 54rem);
    background: var(--sheet);
  }

  /* MapLibre's controls keep their behavior; their surfaces use sheet roles. */
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

  .card {
    position: absolute;
    z-index: 3;
    top: 1rem;
    right: 1rem;
    width: 18rem;
    max-width: calc(100% - 2rem);
    max-height: calc(100% - 10rem);
    overflow-y: auto;
    background: var(--sheet);
  }

  .card-head {
    display: flex;
    align-items: center;
    padding: 0.4rem 0.75rem;
    gap: 0.5rem;
  }

  .card-title {
    flex: 1;
    margin: 0;
    font: 0.75rem/1.5 var(--font-code);
    color: var(--ink);
  }

  .card-head :global(.card-close) {
    flex: none;
    padding: 0;
    font-size: 1.5rem;
    text-decoration: none;
  }

  .card-metric {
    margin: 0 0 1rem;
  }

  .card-metric.spec .row {
    grid-template-columns: 1fr 1fr;
  }

  .card-metric.spec dt,
  .card-metric.spec dd {
    padding: 0.5rem 0.75rem;
  }

  .card-windows {
    width: calc(100% - 1.5rem);
    border-collapse: collapse;
    margin: 0.5rem 0.75rem;
    font: 0.8rem var(--font-code);
  }

  .card-windows th,
  .card-windows td {
    padding: 0.55rem 0;
    text-align: left;
  }

  .card-windows th {
    color: var(--ink-2);
    border-bottom: 1px solid var(--stroke);
    font-size: 0.7rem;
    font-weight: 500;
  }

  .card-windows th:last-child,
  .card-windows td:last-child {
    text-align: right;
  }

  .win-day {
    vertical-align: top;
  }

  .card-windows tbody tr.day-start:not(:first-child) td {
    border-top: 1px solid var(--line);
  }

  .month-chart {
    display: block;
    width: calc(100% - 1.5rem);
    height: auto;
    margin: 0.75rem;
  }

  .chart-axis,
  .chart-bar {
    stroke: var(--stroke);
    stroke-width: 1;
  }

  .chart-bar {
    fill: var(--stars-low);
  }

  .chart-bar.is-max {
    fill: var(--stars-high);
  }

  .chart-val,
  .chart-tick {
    font: 11px var(--font-code);
    fill: var(--ink-2);
    text-anchor: middle;
  }

  .chart-tick {
    font-size: 10px;
  }

  .card-empty {
    margin: 0.75rem;
    font: 0.75rem/1.6 var(--font-code);
    color: var(--ink-2);
  }

  .card-loc {
    display: block;
    padding: 0.8rem 0.75rem;
    background: var(--band);
    border-top: 1px solid var(--stroke);
    font: 0.8rem var(--font-code);
    text-underline-offset: 0.2em;
  }

  .card-loc:hover {
    color: var(--accent-ink);
  }

  .card-loc-arrow {
    margin-left: 0.5rem;
  }

  .legend {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    padding: 0.75rem;
    border-top: 1px solid var(--stroke);
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
  }

  .legend-list {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .legend-list li {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .legend-sw {
    width: 0.8rem;
    height: 0.8rem;
    border: 1px solid var(--ink-2);
    flex: none;
  }

  @media (max-width: 760px) {
    .map {
      height: 28rem;
    }

    /* Details stay in the document on phones, leaving pan and zoom reachable. */
    .card {
      position: static;
      width: auto;
      max-width: none;
      max-height: none;
      border-width: 1px 0 0;
    }
  }
</style>
