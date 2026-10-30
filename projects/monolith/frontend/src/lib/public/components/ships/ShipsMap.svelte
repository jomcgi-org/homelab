<script>
  import { onMount } from "svelte";
  import { Button } from "@homelab/design-system/components";
  import {
    readPalette,
    paintBasemap,
    HEAT_STEPS,
  } from "$lib/public/ships/palette.js";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import "maplibre-gl/dist/maplibre-gl.css";
  import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
  import { deadReckon } from "$lib/public/ships/deadReckoning.js";
  import {
    readShipsParams,
    writeShipsParams,
  } from "$lib/public/ships/urlParams.js";

  let { vessels = [] } = $props();

  // Keep the existing provider and attribution. Paint follows the scoped sheet.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  // A fix older than this is almost certainly a vessel that stopped
  // reporting; dead-reckoning it any further would fling the marker across
  // the map, so clamp the extrapolation window.
  const MAX_DR_SECONDS = 300;

  // Vessels render as a GPU symbol layer (not DOM markers): the points are
  // composited with the basemap on the GPU, so they move in lockstep during
  // pan/zoom instead of trailing a frame behind. Dead-reckoning rebuilds the
  // source on a throttled cadence rather than per-frame.
  const SOURCE_ID = "vessels";
  const LAYER_ID = "vessels";
  const DR_INTERVAL_MS = 250; // ~4 Hz; ships move ~10 m/s, so this is smooth

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let pal = null; // resolved on the map surface at load and on scheme changes
  let rafId = null;
  let lastDrMs = 0;
  let didFit = false;
  let layerReady = false;
  let disposed = false;
  let heatRequest = null;
  let trackRequest = null;

  // mmsi -> { row, fixLat, fixLon, fixTimeMs, speed, course, rotation, icon }
  // `row` is the original snapshot object, kept for the detail panel.
  const fleet = new Map();

  let selectedMmsi = null; // string mmsi of the highlighted vessel
  let hoveredMmsi = null; // string mmsi under the cursor

  let selected = $state(null); // selected vessel object (snapshot row)
  let track = $state(null); // { mmsi, count, track } or null
  let trackLoading = $state(false);

  // Vessel-type filter. Each legend box toggles whether that ITU band shows on
  // the map; toggling sets a MapLibre layer filter on the `icon` property.
  const LEGEND = [
    {
      key: "passenger",
      label: "Passenger",
      icon: "ship-passenger",
      color: "var(--ships-passenger)",
    },
    {
      key: "cargo",
      label: "Cargo",
      icon: "ship-cargo",
      color: "var(--ships-cargo)",
    },
    {
      key: "tanker",
      label: "Tanker",
      icon: "ship-tanker",
      color: "var(--ships-tanker)",
    },
    {
      key: "hsc",
      label: "High-speed",
      icon: "ship-hsc",
      color: "var(--ships-hsc)",
    },
    {
      key: "special",
      label: "Special",
      icon: "ship-special",
      color: "var(--ships-special)",
    },
    {
      key: "unknown",
      label: "Other",
      icon: "ship-unknown",
      color: "var(--ships-unknown)",
    },
  ];
  // The full legend order, used as the vessel-type allow-list + "all on" default
  // for URL sync, and to write `types` in a stable order.
  const LEGEND_KEYS = LEGEND.map((l) => l.key);

  // View state (type filter, mode, selection) is initialized from the URL on
  // load (so a shared link restores it) and mirrored back as it changes (see the
  // $effect below). This is a lib component, but $app/stores + $app/navigation
  // work in any client component. ShipsMap only mounts inside the route's
  // browser guard, so reading $page here is always client-side.
  const initialView = readShipsParams($page.url.searchParams, LEGEND_KEYS);

  let active = $state(initialView.active); // enabled vessel types (all on by default)

  // A vessel selection restored from the URL (?mmsi=), applied once its row
  // arrives in the fleet (see syncVessels). Cleared after it is consumed so a
  // later user-driven deselect is not undone.
  let pendingMmsi = initialView.mmsi;

  function applyFilter() {
    if (!map || !layerReady) return;
    if (active.size === LEGEND.length) {
      map.setFilter(LAYER_ID, null); // everything visible: no filter
      return;
    }
    const icons = LEGEND.filter((l) => active.has(l.key)).map((l) => l.icon);
    map.setFilter(LAYER_ID, ["in", ["get", "icon"], ["literal", icons]]);
  }

  function toggleType(key) {
    // Reassign (not mutate) so the $state Set re-renders the legend.
    const next = new Set(active);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    active = next;
    applyFilter();
  }

  // ── Traffic-density heatmap ────────────────────────────────────────────
  // 'vessels' shows the live markers; 'heat' hides them and shows a GPU fill
  // layer of ~500m cells coloured by how many distinct moving vessels have used
  // each, cumulatively over all time (vessel-days, not a sliding window). The
  // grid is fetched once, on the first toggle.
  const HEAT_SOURCE = "heat-cells";
  const HEAT_LAYER = "heat-fill";

  // Quantile stops come from the backend. The scheme-aware sequential ramp
  // and its legend share the sheet's --ships-heat-* roles. Older cached or
  // empty payloads retain the established fallback breaks.
  const FALLBACK_BREAKS = [1, 3, 6, 10, 15, 20];

  // MapLibre `step` wants STRICTLY ASCENDING stop inputs: first the default
  // color (count < breaks[1]), then (stopValue, color) pairs. The backend
  // guarantees `stops` is ascending+unique and FALLBACK_BREAKS is too, so the
  // expression is always valid.
  function rampFor(stops) {
    const breaks = stops && stops.length ? stops : FALLBACK_BREAKS;
    const expr = ["step", ["get", "count"], pal.heat[0]];
    for (let i = 1; i < breaks.length && i < HEAT_STEPS; i++) {
      expr.push(breaks[i], pal.heat[i]);
    }
    return { expr, breaks };
  }

  // Legend label for bucket i: "lo-hi" for interior buckets, "lo" when the next
  // break is adjacent (no range), "lo+" for the open-ended top bucket.
  function heatLabel(breaks, i) {
    if (i >= breaks.length - 1) return `${breaks[i]}+`;
    const hi = breaks[i + 1] - 1;
    return hi < breaks[i] ? `${breaks[i]}` : `${breaks[i]}-${hi}`;
  }

  let mode = $state(initialView.mode); // 'vessels' | 'heat'
  let heatLoaded = false;
  // Drives the legend; updated from data.stops once the grid loads, so the
  // swatches/labels track the live quantile breaks (fallback until then).
  let heatBreaks = $state(FALLBACK_BREAKS);

  // Build square cell polygons from the compact API payload
  // ({step_lat, step_lon, cells:[[lat_bin, lon_bin, count]]}).
  function buildHeatFC(data) {
    const sLat = data.step_lat;
    const sLon = data.step_lon;
    const features = (data.cells || []).map(([la, lo, c]) => {
      const lat0 = la * sLat;
      const lon0 = lo * sLon;
      const lat1 = lat0 + sLat;
      const lon1 = lon0 + sLon;
      return {
        type: "Feature",
        geometry: {
          type: "Polygon",
          coordinates: [
            [
              [lon0, lat0],
              [lon1, lat0],
              [lon1, lat1],
              [lon0, lat1],
              [lon0, lat0],
            ],
          ],
        },
        properties: { count: c },
      };
    });
    return { type: "FeatureCollection", features };
  }

  async function loadHeat() {
    if (heatLoaded || heatRequest || disposed || !layerReady) return;
    const request = new AbortController();
    heatRequest = request;
    try {
      // Same-origin proxy to /api/ships/heat (see heat/+server.js).
      const res = await fetch("/app/ships/heat", { signal: request.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (disposed || request.signal.aborted) return;
      const { expr, breaks } = rampFor(data.stops);
      map.setPaintProperty(HEAT_LAYER, "fill-color", expr);
      heatBreaks = breaks; // re-render the legend from the live breaks
      map.getSource(HEAT_SOURCE)?.setData(buildHeatFC(data));
      heatLoaded = true;
    } catch {
      // Leave the grid empty on failure; the toggle still works.
    } finally {
      if (heatRequest === request) heatRequest = null;
    }
  }

  // Apply the layer visibility for `mode` to the map. Extracted so it can run
  // both on a user toggle (setMode) and once at load when the URL restored a
  // non-default mode (applyInitialMode in the load handler).
  function applyModeLayers(next) {
    if (!map || !layerReady) return;
    const heat = next === "heat";
    if (heat) loadHeat();
    map.setLayoutProperty(HEAT_LAYER, "visibility", heat ? "visible" : "none");
    map.setLayoutProperty(LAYER_ID, "visibility", heat ? "none" : "visible");
    map.setLayoutProperty(
      "ship-track-line",
      "visibility",
      heat ? "none" : "visible",
    );
    if (heat) closePanel(); // markers hidden, so drop any open vessel panel
  }

  function setMode(next) {
    if (next === mode || !map || !layerReady) return;
    mode = next;
    applyModeLayers(next);
  }

  function emptyFC() {
    return { type: "FeatureCollection", features: [] };
  }

  function refreshPalette() {
    if (!map || !layerReady) return;
    pal = readPalette(mapContainer);
    paintBasemap(map, pal);
    registerIcons(pal);
    map.setPaintProperty(HEAT_LAYER, "fill-color", rampFor(heatBreaks).expr);
    map.setPaintProperty("ship-track-line", "line-color", pal.ink);
  }

  // ITU AIS ship_type bands mapped onto the icon ids registered below.
  function iconFor(type) {
    if (type == null) return "ship-unknown";
    if (type >= 60 && type <= 69) return "ship-passenger";
    if (type >= 70 && type <= 79) return "ship-cargo";
    if (type >= 80 && type <= 89) return "ship-tanker";
    if (type >= 40 && type <= 49) return "ship-hsc";
    if (type >= 50 && type <= 59) return "ship-special";
    return "ship-unknown";
  }

  function shipTypeLabel(type) {
    if (type == null) return "Unknown";
    if (type >= 60 && type <= 69) return "Passenger";
    if (type >= 70 && type <= 79) return "Cargo";
    if (type >= 80 && type <= 89) return "Tanker";
    if (type >= 40 && type <= 49) return "High-speed craft";
    if (type >= 50 && type <= 59) return "Special craft";
    if (type >= 30 && type <= 39) return "Fishing / working";
    return `Type ${type}`;
  }

  function fmtSpeed(s) {
    return s == null ? "n/a" : `${s.toFixed(1)} kn`;
  }

  function fmtDeg(d) {
    return d == null ? "n/a" : `${Math.round(d)}°`;
  }

  // Base time for extrapolation: the AIS message timestamp, not "now". The
  // snapshot can be served from cache, so recorded_at is the real fix age.
  function fixTimeMs(v) {
    const t = Date.parse(v.recorded_at || v.updated_at || "");
    return Number.isNaN(t) ? Date.now() : t;
  }

  function rotationOf(v) {
    return v.heading ?? v.course ?? 0;
  }

  // Rasterize the ship arrow per palette color and update registered images
  // in place on scheme changes. Per-color icons let a single data-driven
  // `icon-image` expression pick the right tint with no SDF machinery.
  function registerIcons(p) {
    const colors = {
      "ship-passenger": p.passenger,
      "ship-cargo": p.cargo,
      "ship-tanker": p.tanker,
      "ship-hsc": p.hsc,
      "ship-special": p.special,
      "ship-unknown": p.unknown,
    };
    const ratio = 2; // render @2x for crisp icons on HiDPI displays
    const box = 22; // logical icon size in px
    const px = box * ratio;
    for (const [id, fill] of Object.entries(colors)) {
      const canvas = document.createElement("canvas");
      canvas.width = px;
      canvas.height = px;
      const ctx = canvas.getContext("2d");
      ctx.scale(px / 20, px / 20); // path authored in a 20x20 viewBox
      const path = new Path2D("M10 1 L17 18 L10 14 L3 18 Z");
      ctx.fillStyle = fill;
      ctx.fill(path);
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.strokeStyle = p.ink;
      ctx.stroke(path);
      const image = ctx.getImageData(0, 0, px, px);
      if (map.hasImage(id)) map.updateImage(id, image);
      else map.addImage(id, image, { pixelRatio: ratio });
    }
  }

  // Build the FeatureCollection from current fleet state, dead-reckoning each
  // vessel forward from its last fix. selection/hover ride along as feature
  // properties (not feature-state, which setData would clear).
  function buildFeatures() {
    const now = Date.now();
    const features = [];
    for (const [id, e] of fleet) {
      const elapsed = Math.min((now - e.fixTimeMs) / 1000, MAX_DR_SECONDS);
      const pos = deadReckon(
        { lat: e.fixLat, lon: e.fixLon, speed: e.speed, course: e.course },
        elapsed,
      );
      features.push({
        type: "Feature",
        id,
        geometry: { type: "Point", coordinates: [pos.lon, pos.lat] },
        properties: {
          mmsi: id,
          icon: e.icon,
          rotation: e.rotation,
          sel: id === selectedMmsi,
          hov: id === hoveredMmsi,
        },
      });
    }
    return { type: "FeatureCollection", features };
  }

  function pushData() {
    const src = map?.getSource(SOURCE_ID);
    if (src) src.setData(buildFeatures());
  }

  // AIS sentinels: lat 91 / lon 181 mean "position not available"; (0, 0) is
  // null island. Drop these so they neither plot nor blow out the bounds.
  function validLatLon(lat, lon) {
    if (lat == null || lon == null) return false;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return false;
    if (lat === 0 && lon === 0) return false;
    return true;
  }

  // Linear-interpolated quantile over a pre-sorted array.
  function quantile(sorted, q) {
    const pos = (sorted.length - 1) * q;
    const base = Math.floor(pos);
    const next = sorted[base + 1];
    return next === undefined
      ? sorted[base]
      : sorted[base] + (pos - base) * (next - sorted[base]);
  }

  // Bounds framing the dense core (5th-95th percentile) of a snapshot, as a
  // [[w, s], [e, n]] LngLatBoundsLike. The AIS feed is regionally clustered, so
  // a handful of near-outliers would pull the full spread wide and leave the
  // screen mostly empty water; trimming to the core lands the view on where the
  // vessels actually are (the outliers still render, reachable by panning out).
  // Returns null when there are no plottable fixes. Used both to seed the map's
  // initial viewport at construction (so the world never flashes before the fit)
  // and as the late fallback fit when the first snapshot arrives after mount.
  function coreBounds(list) {
    const lats = [];
    const lons = [];
    for (const v of list) {
      if (!validLatLon(v.lat, v.lon)) continue;
      lats.push(v.lat);
      lons.push(v.lon);
    }
    if (!lats.length) return null;
    lats.sort((a, b) => a - b);
    lons.sort((a, b) => a - b);
    const q = 0.05;
    return [
      [quantile(lons, q), quantile(lats, q)],
      [quantile(lons, 1 - q), quantile(lats, 1 - q)],
    ];
  }

  // Shared fitBounds tuning. maxZoom 12 lets a tight cluster fill the viewport
  // instead of being capped far out; fitBounds only reaches it when the core
  // really is that small, so a wider fleet still frames naturally.
  const FIT_OPTIONS = { padding: 48, maxZoom: 12 };

  // Reset fleet state to snapshot truth whenever a new snapshot arrives, then
  // repaint. `vessels` is a fresh array on every invalidateAll.
  function syncVessels() {
    if (!map || !layerReady) return;

    const seen = new Set();
    for (const v of vessels) {
      if (!validLatLon(v.lat, v.lon)) continue;
      const id = String(v.mmsi);
      seen.add(id);
      fleet.set(id, {
        row: v,
        fixLat: v.lat,
        fixLon: v.lon,
        fixTimeMs: fixTimeMs(v),
        speed: v.speed,
        course: v.course,
        rotation: rotationOf(v),
        icon: iconFor(v.ship_type),
      });
    }
    for (const id of fleet.keys()) {
      if (!seen.has(id)) fleet.delete(id);
    }

    pushData();

    // Restore a URL-shared vessel selection once its row is in the fleet. Only
    // fires once (pendingMmsi is cleared), so a later user deselect sticks. If
    // the mmsi is not in this snapshot, drop it rather than wait indefinitely.
    if (pendingMmsi) {
      const entry = fleet.get(pendingMmsi);
      pendingMmsi = null;
      if (entry) selectVessel(entry.row);
    }

    // Late fallback fit: only reached when the map was created without a seed
    // viewport (an empty snapshot at mount) and the first vessels arrived on a
    // later refresh. The common path seeds the viewport at construction (see
    // onMount), so didFit is already true and this is skipped, no snap.
    if (!didFit && seen.size > 0) {
      const b = coreBounds(vessels);
      if (b) {
        map.fitBounds(b, { ...FIT_OPTIONS, duration: 0 });
        didFit = true;
      }
    }
  }

  // Throttled dead-reckoning: rebuild the source ~4x/sec. During pan/zoom the
  // points already track the basemap on the GPU, so this only animates the
  // slow drift between snapshots.
  function startLoop() {
    const step = () => {
      const now = Date.now();
      if (now - lastDrMs >= DR_INTERVAL_MS) {
        lastDrMs = now;
        pushData();
      }
      rafId = requestAnimationFrame(step);
    };
    rafId = requestAnimationFrame(step);
  }

  function drawTrack(points) {
    const src = map?.getSource("ship-track");
    if (!src) return;
    const coordinates = (points || [])
      .filter((p) => p.lon != null && p.lat != null)
      .map((p) => [p.lon, p.lat]);
    src.setData({
      type: "Feature",
      geometry: { type: "LineString", coordinates },
      properties: {},
    });
  }

  async function selectVessel(v) {
    trackRequest?.abort();
    const request = new AbortController();
    trackRequest = request;
    selected = v;
    selectedMmsi = String(v.mmsi);
    pushData(); // highlight immediately, don't wait for the next DR tick
    track = null;
    drawTrack([]);
    trackLoading = true;
    try {
      // Same-origin site route, proxied to /api/ships/track/{mmsi} server-side
      // by +server.js. The browser never touches the private API surface.
      const res = await fetch(
        `/app/ships/track/${encodeURIComponent(v.mmsi)}`,
        { signal: request.signal },
      );
      if (res.ok) {
        const body = await res.json();
        if (disposed || request.signal.aborted || trackRequest !== request)
          return;
        track = body;
        drawTrack(body.track);
      }
    } catch {
      // Network blip: keep the panel open with snapshot fields, just no track.
    } finally {
      if (trackRequest === request) {
        trackLoading = false;
        trackRequest = null;
      }
    }
  }

  function closePanel() {
    trackRequest?.abort();
    trackRequest = null;
    trackLoading = false;
    selected = null;
    selectedMmsi = null;
    track = null;
    pushData();
    const src = map?.getSource("ship-track");
    if (src) src.setData(emptyFC());
  }

  // Reset to snapshot truth whenever a new snapshot arrives.
  $effect(() => {
    void vessels;
    if (map && layerReady) syncVessels();
  });

  // Mirror the view state (type filter, mode, selected vessel) back to the URL
  // so it is shareable. replaceState so toggling types/mode and tapping vessels
  // does not spam browser history. Guarded: only goto when the serialized params
  // differ from the current URL, so this "URL write" never re-triggers the init
  // read in a loop.
  $effect(() => {
    const url = new URL($page.url);
    writeShipsParams(
      url.searchParams,
      { active, mode, mmsi: selected?.mmsi ?? null },
      LEGEND_KEYS,
    );
    if (url.searchParams.toString() !== $page.url.searchParams.toString()) {
      goto(url, { keepFocus: true, noScroll: true, replaceState: true });
    }
  });

  onMount(() => {
    let cleanup = () => {};
    let destroyed = false;
    let warmup;
    let idleWarmup = false;

    (async () => {
      maplibregl = await import("maplibre-gl");
      if (destroyed) return;
      // Vite must bundle the worker's shared imports too. The package-relative
      // default URL otherwise points at a missing asset in a production build.
      maplibregl.setWorkerUrl(workerUrl);

      // Seed the initial viewport from the SSR snapshot so the map's first paint
      // is already framed on the fleet. Without this the map opened at a whole-
      // world view and only snapped to the fleet once syncVessels ran, so you'd
      // see the world flash up and then jump-zoom in. The snapshot is present at
      // mount (passed as a prop from the server load), so the framing is known
      // before the map exists. Falls back to a wide world view only when the
      // snapshot is empty; that late-arriving case is fit by syncVessels.
      const seedBounds = coreBounds(vessels);

      map = new maplibregl.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
        ...(seedBounds
          ? { bounds: seedBounds, fitBoundsOptions: FIT_OPTIONS }
          : { center: [0, 30], zoom: 1.4 }),
        // Render the OSM/OpenMapTiles attribution as a compact "i" button
        // instead of letting the full credit line sprawl across the bottom of
        // the map (especially cramped on mobile). MapLibre opens this expanded
        // on first paint and only minimizes it once the user pans, so we also
        // collapse it up front below; tapping the button still reveals the
        // required credits.
        attributionControl: { compact: true },
      });
      // The viewport is already framed on the fleet, so suppress the one-time
      // fit in syncVessels (it would otherwise snap to the same bounds again).
      if (seedBounds) didFit = true;
      map.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );

      // MapLibre's compact AttributionControl opens itself (a <details> with
      // the `open` attribute + `maplibregl-compact-show` class) every time the
      // style's attributions (re)populate, so it keeps wanting to render as a
      // full-width credit bar. The old fix stripped that once on `idle`, but a
      // live ships map never reliably idles: vessel dead-reckoning rewrites the
      // source ~4x/sec and tiles keep streaming, so on a real session `idle`
      // may never fire and the bar is left expanded (and on narrow/edge layouts
      // that wide bar is what spills past the corner). Collapse it on every
      // attribution update instead, so it never paints expanded, and stop once
      // the user taps the button open themselves (their click sets `open`,
      // which we then leave alone so the required OSM credits stay readable).
      let userOpenedAttrib = false;
      const collapseAttrib = () => {
        if (userOpenedAttrib) return;
        const attrib = mapContainer.querySelector(".maplibregl-ctrl-attrib");
        attrib?.removeAttribute("open");
        attrib?.classList.remove("maplibregl-compact-show");
      };
      const onAttributionClick = (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      };
      mapContainer.addEventListener("click", onAttributionClick);
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

      map.on("load", () => {
        if (disposed || layerReady) return;
        pal = readPalette(mapContainer);
        paintBasemap(map, pal);
        registerIcons(pal);

        // Traffic-density heatmap, hidden until toggled into Heat mode. Added
        // first so it sits beneath the track + markers (the modes are mutually
        // exclusive anyway, so it never actually overlaps them).
        map.addSource(HEAT_SOURCE, { type: "geojson", data: emptyFC() });
        map.addLayer({
          id: HEAT_LAYER,
          type: "fill",
          source: HEAT_SOURCE,
          layout: { visibility: "none" },
          paint: {
            // Placeholder fallback ramp; replaced with the data-derived ramp in
            // loadHeat() once the grid (and its quantile breaks) arrives.
            "fill-color": rampFor(null).expr,
            // Opaque cells match the legend exactly in either scheme.
            "fill-opacity": 1,
          },
        });

        map.addSource("ship-track", { type: "geojson", data: emptyFC() });
        map.addLayer({
          id: "ship-track-line",
          type: "line",
          source: "ship-track",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": pal.ink,
            "line-width": 2.5,
            "line-dasharray": [1.5, 1],
          },
        });

        map.addSource(SOURCE_ID, { type: "geojson", data: emptyFC() });
        map.addLayer({
          id: LAYER_ID,
          type: "symbol",
          source: SOURCE_ID,
          layout: {
            "icon-image": ["get", "icon"],
            "icon-rotate": ["get", "rotation"],
            "icon-rotation-alignment": "map",
            "icon-allow-overlap": true,
            "icon-ignore-placement": true,
            // Grow the selected vessel, then the hovered one.
            "icon-size": [
              "case",
              ["get", "sel"],
              1.5,
              ["get", "hov"],
              1.25,
              1.0,
            ],
          },
        });
        layerReady = true;
        applyFilter(); // honor any pre-toggled legend state
        // Honor a URL-restored mode (e.g. ?mode=heat); vessels is the layer
        // default, so this only does work when heat was shared. In heat mode the
        // markers hide, so any restored selection is dropped (see applyModeLayers
        // -> closePanel) and the pending-mmsi restore is cancelled.
        if (mode !== "vessels") {
          pendingMmsi = null;
          applyModeLayers(mode);
        }

        map.on("click", LAYER_ID, (e) => {
          const f = e.features?.[0];
          if (!f) return;
          const entry = fleet.get(String(f.id));
          if (entry) selectVessel(entry.row);
        });
        map.on("mouseenter", LAYER_ID, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mousemove", LAYER_ID, (e) => {
          const id = e.features?.[0] ? String(e.features[0].id) : null;
          if (id !== hoveredMmsi) {
            hoveredMmsi = id;
            pushData();
          }
        });
        map.on("mouseleave", LAYER_ID, () => {
          map.getCanvas().style.cursor = "";
          if (hoveredMmsi !== null) {
            hoveredMmsi = null;
            pushData();
          }
        });

        syncVessels();
        startLoop();

        // Warm the heat grid in the background so the first Heat toggle is
        // instant. Still lazy (kept out of the initial payload), just fetched
        // on idle after first paint instead of waiting for the toggle.
        // loadHeat() is a no-op once cached, so the toggle handler stays correct.
        if (typeof requestIdleCallback === "function") {
          idleWarmup = true;
          warmup = requestIdleCallback(() => loadHeat(), { timeout: 3000 });
        } else {
          warmup = setTimeout(() => loadHeat(), 1500);
        }
      });

      const scheme = new MutationObserver(refreshPalette);
      scheme.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
      const media = window.matchMedia("(prefers-color-scheme: dark)");
      media.addEventListener("change", refreshPalette);

      cleanup = () => {
        scheme.disconnect();
        media.removeEventListener("change", refreshPalette);
        mapContainer.removeEventListener("click", onAttributionClick);
        if (idleWarmup) cancelIdleCallback(warmup);
        else clearTimeout(warmup);
        heatRequest?.abort();
        trackRequest?.abort();
        if (rafId) cancelAnimationFrame(rafId);
        fleet.clear();
        map?.remove();
        map = null;
        layerReady = false;
      };
    })();

    return () => {
      destroyed = true;
      disposed = true;
      cleanup();
    };
  });
</script>

<div
  class="map-wrap"
  class:heat-mode={mode === "heat"}
  class:panel-open={selected}
>
  <div class="map" bind:this={mapContainer}></div>

  <div class="mode-toggle" role="group" aria-label="Map mode">
    <Button
      variant="secondary"
      aria-pressed={mode === "vessels"}
      onclick={() => setMode("vessels")}
    >
      Vessels
    </Button>
    <Button
      variant="secondary"
      aria-pressed={mode === "heat"}
      onclick={() => setMode("heat")}
    >
      Heat
    </Button>
  </div>

  {#if mode === "vessels"}
    <div class="legend edition">
      <p class="ed-head legend-title">Vessel type</p>
      <div class="legend-grid">
        {#each LEGEND as item (item.key)}
          <Button
            variant="secondary"
            class="legend-item"
            style="--vessel-color: {item.color}"
            aria-pressed={active.has(item.key)}
            onclick={() => toggleType(item.key)}
          >
            <span class="vessel-sw" aria-hidden="true"></span>
            <span class="legend-label">{item.label}</span>
          </Button>
        {/each}
      </div>
    </div>
  {:else}
    <div class="legend edition">
      <p class="ed-head legend-title">Ships seen per map square, all time</p>
      <ul class="heat-scale">
        {#each heatBreaks.slice(0, HEAT_STEPS) as br, i (br)}
          <li>
            <span class="heat-sw" style="background: var(--ships-heat-{i + 1})"
            ></span>{heatLabel(heatBreaks, i)}
          </li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if selected}
    <aside class="panel edition" aria-label="Vessel details">
      <Button
        variant="secondary"
        class="panel-close"
        onclick={closePanel}
        aria-label="Close vessel panel">&times;</Button
      >
      <h2 class="panel-name">
        {selected.name ||
          selected.ship_name ||
          `MMSI ${selected.mmsi} (ship radio id)`}
      </h2>
      <dl class="panel-rows spec">
        <div class="row">
          <dt title="a ship's radio id">MMSI</dt>
          <dd>{selected.mmsi}</dd>
        </div>
        <div class="row">
          <dt>Type</dt>
          <dd>{shipTypeLabel(selected.ship_type)}</dd>
        </div>
        <div class="row">
          <dt>Speed</dt>
          <dd>{fmtSpeed(selected.speed)}</dd>
        </div>
        <div class="row">
          <dt>Course</dt>
          <dd>{fmtDeg(selected.course)}</dd>
        </div>
        <div class="row">
          <dt>Heading</dt>
          <dd>{fmtDeg(selected.heading)}</dd>
        </div>
        <div class="row">
          <dt>Destination</dt>
          <dd>{selected.destination || "Unknown"}</dd>
        </div>
      </dl>
      {#if trackLoading}
        <p class="panel-meta">Loading track…</p>
      {:else if track}
        <p class="panel-meta">{track.count} track points</p>
      {/if}
    </aside>
  {/if}
</div>

<style>
  .map-wrap {
    position: absolute;
    inset: 0;
    overflow: hidden;
    background: var(--sheet);
  }
  .map {
    position: absolute;
    inset: 0;
  }

  /* Keep the provider's controls and attribution, with flat sheet styling and
     44px pointer targets. Safe-area insets protect the bottom/right stack. */
  .map :global(.maplibregl-ctrl-bottom-right) {
    bottom: env(safe-area-inset-bottom, 0);
    right: env(safe-area-inset-right, 0);
  }
  .map :global(.maplibregl-ctrl-group),
  .map :global(.maplibregl-ctrl-attrib) {
    border: 1px solid var(--stroke);
    border-radius: 0;
    background: var(--sheet);
    box-shadow: none;
    color: var(--ink);
  }
  .map :global(.maplibregl-ctrl-group button),
  .map :global(.maplibregl-ctrl-attrib-button) {
    width: 44px;
    height: 44px;
    border-radius: 0;
    background: var(--sheet);
    color: var(--ink);
  }
  .map :global(.maplibregl-ctrl-group button + button) {
    border-top: 1px solid var(--stroke);
  }
  /* Text glyphs inherit ink in both schemes, unlike the provider's black PNGs. */
  .map :global(.maplibregl-ctrl-icon) {
    background-image: none;
  }
  .map :global(.maplibregl-ctrl-zoom-in)::after {
    content: "+";
  }
  .map :global(.maplibregl-ctrl-zoom-out)::after {
    content: "−";
  }
  .map :global(.maplibregl-ctrl-group button)::after {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    font: 24px var(--font-code);
  }
  .map :global(.maplibregl-ctrl-group button) {
    position: relative;
  }
  .map :global(.maplibregl-ctrl-attrib.maplibregl-compact) {
    min-height: 44px;
    padding: 0 44px 0 0;
  }
  .map :global(.maplibregl-ctrl-attrib-button)::before {
    content: "i";
    background: none;
    font: 20px var(--font-code);
    position: static;
    width: auto;
    height: auto;
    margin: 0;
  }
  .map :global(.maplibregl-ctrl-attrib-button) {
    display: grid;
    place-items: center;
    list-style: none;
  }
  .map :global(.maplibregl-ctrl-attrib-inner) {
    padding: 0.5rem;
    font: 11px var(--font-code);
    max-width: min(300px, calc(100vw - 70px));
  }
  .map :global(.maplibregl-ctrl-attrib-inner a) {
    color: var(--ink);
  }
  .map :global(button:focus-visible),
  .map :global(summary:focus-visible) {
    outline: 2px solid var(--accent-ink);
    outline-offset: -3px;
  }

  .mode-toggle {
    position: absolute;
    top: 12px;
    right: 12px;
    display: flex;
    font: 12px var(--font-code);
  }
  .mode-toggle :global(button + button) {
    border-left: 0;
  }
  .mode-toggle :global(button[aria-pressed="true"]) {
    background: var(--ink);
    color: var(--sheet);
  }
  .legend {
    position: absolute;
    bottom: 12px;
    left: 12px;
    width: 264px;
    max-width: calc(100% - 82px);
    background: var(--sheet);
  }
  .legend-title {
    margin: 0;
    padding: 0.5rem;
    font: 12px var(--font-code);
  }
  .legend-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .legend-grid :global(.legend-item) {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border: 0;
    border-top: 1px solid var(--stroke);
    font: 12px var(--font-code);
    text-align: left;
  }
  .legend-grid :global(.legend-item:nth-child(even)) {
    border-left: 1px solid var(--stroke);
  }
  .vessel-sw,
  .heat-sw {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 1px solid var(--ink-2);
    flex: none;
  }
  .vessel-sw {
    background: var(--vessel-color);
  }
  .legend-grid :global(button[aria-pressed="false"] .vessel-sw) {
    background: var(--sheet);
  }
  .legend-grid :global(button[aria-pressed="false"] .legend-label) {
    text-decoration: line-through;
  }
  .heat-scale {
    display: grid;
    grid-template-columns: 1fr 1fr;
    padding: 10px;
    margin: 0;
    gap: 10px;
    list-style: none;
    font: 12px var(--font-code);
  }
  .heat-scale li {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .panel {
    position: absolute;
    top: 68px;
    right: 12px;
    width: 300px;
    max-width: calc(100% - 24px);
    max-height: calc(100% - 220px);
    overflow-y: auto;
    background: var(--sheet);
  }
  .panel :global(.panel-close) {
    float: right;
    margin: 6px;
    font-size: 22px;
  }
  .panel-name {
    margin: 0;
    padding: 12px;
    font: 700 1.15rem var(--font-ui);
    overflow-wrap: anywhere;
  }
  .panel-rows {
    clear: both;
    margin: 0;
  }
  .panel-rows dt {
    color: var(--ink-2);
  }
  .panel-rows dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .panel-meta {
    margin: 0;
    padding: 10px 12px;
    font: 12px var(--font-code);
    border-top: 1px solid var(--stroke);
    color: var(--ink-2);
  }
  @media (max-width: 640px) {
    .panel-open .legend {
      display: none;
    }
    .panel {
      top: auto;
      bottom: 0;
      left: 0;
      right: 0;
      width: auto;
      max-width: none;
      max-height: 50%;
      padding-bottom: env(safe-area-inset-bottom, 0);
      border-width: 1px 0 0;
    }
    /* The details occupy the bottom half; zoom/attribution stay in open water. */
    .panel-open .map :global(.maplibregl-ctrl-bottom-right) {
      bottom: 51%;
    }
  }
</style>
