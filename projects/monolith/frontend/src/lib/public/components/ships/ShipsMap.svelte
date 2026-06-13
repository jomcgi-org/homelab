<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import { deadReckon } from "$lib/public/ships/deadReckoning.js";

  let { vessels = [] } = $props();

  // OpenFreeMap needs no API key. Self-hosting the tiles + style is a
  // follow-up; for now we point at the hosted liberty style and tint the
  // canvas toward the cream palette with a CSS filter (see <style>).
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
  let pal = null; // design-token palette, resolved once the map is up
  let rafId = null;
  let lastDrMs = 0;
  let didFit = false;
  let layerReady = false;

  // mmsi -> { row, fixLat, fixLon, fixTimeMs, speed, course, rotation, icon }
  // `row` is the original snapshot object, kept for the detail panel.
  const fleet = new Map();

  let selectedMmsi = null; // string mmsi of the highlighted vessel
  let hoveredMmsi = null; // string mmsi under the cursor

  let selected = $state(null); // selected vessel object (snapshot row)
  let track = $state(null); // { mmsi, count, track } or null
  let trackLoading = $state(false);

  // Vessel-type palette. A vibrant MotherDuck-flavored set used for both the
  // map markers and the legend boxes. Kept local (not the global --blue/--teal
  // tokens) so the map pops without churning the site's restrained palette: the
  // muddy --teal/--ink-3 read as dull behind the basemap's grayscale filter.
  const VESSEL_COLORS = {
    passenger: "#3b8ef5", // azure
    cargo: "#14c4a9", // aqua-mint (distinct from the green Special)
    tanker: "#ff564a", // coral-red
    hsc: "#ffcb1f", // golden
    special: "#35cb5b", // spring green
    unknown: "#8b6fe8", // periwinkle violet (completes the spectrum)
  };

  // Vessel-type filter. Each legend box toggles whether that ITU band shows on
  // the map; toggling sets a MapLibre layer filter on the `icon` property.
  const LEGEND = [
    { key: "passenger", label: "Passenger", icon: "ship-passenger", color: VESSEL_COLORS.passenger },
    { key: "cargo", label: "Cargo", icon: "ship-cargo", color: VESSEL_COLORS.cargo },
    { key: "tanker", label: "Tanker", icon: "ship-tanker", color: VESSEL_COLORS.tanker },
    { key: "hsc", label: "High-speed", icon: "ship-hsc", color: VESSEL_COLORS.hsc },
    { key: "special", label: "Special", icon: "ship-special", color: VESSEL_COLORS.special },
    { key: "unknown", label: "Other", icon: "ship-unknown", color: VESSEL_COLORS.unknown },
  ];
  let active = $state(new Set(LEGEND.map((l) => l.key))); // all on by default

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
  // layer of ~500m cells coloured by how many distinct moving vessels used each
  // over the last 7 days. The grid is fetched once, on the first toggle.
  const HEAT_SOURCE = "heat-cells";
  const HEAT_LAYER = "heat-fill";

  // Neo-brutalist stepped ramp, tuned to the distinct-mover counts measured on
  // live data (p50=1, p90=3, p99=10, max~22): quiet water -> busy lanes.
  // Fully saturated plasma scale (vivid violet through magenta and rose to
  // orange/red). Deliberately skips green and pale blue so even the quiet-water
  // low end stays vibrant against the green-and-beige basemap when zoomed out.
  // Brightness climbs monotonically to the top so the busiest lanes read as the
  // BRIGHTEST red, not a dark maroon. The old ramp topped out at a deep #c1121f
  // that was darker than the step below it, which (compounded by the basemap's
  // grayscale CSS filter, see <style>) made busy water look muddy instead of
  // hot. The high end is now a pure vivid red.
  const HEAT_STOPS = [
    { at: 1, color: "#7b2ff7", label: "1-2" }, // vivid violet
    { at: 3, color: "#d61f9c", label: "3-5" }, // magenta
    { at: 6, color: "#ff0a78", label: "6-9" }, // hot rose
    { at: 10, color: "#ff6a00", label: "10-14" }, // vivid orange
    { at: 15, color: "#ff2a1f", label: "15-19" }, // bright red
    { at: 20, color: "#ff0019", label: "20+" }, // pure vivid red (hottest)
  ];
  const HEAT_FILL_COLOR = [
    "step",
    ["get", "count"],
    HEAT_STOPS[0].color,
    ...HEAT_STOPS.slice(1).flatMap((s) => [s.at, s.color]),
  ];

  let mode = $state("vessels"); // 'vessels' | 'heat'
  let heatLoaded = false;

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
    if (heatLoaded) return;
    try {
      // Same-origin proxy to /api/ships/heat (see heat/+server.js).
      const res = await fetch("/app/ships/heat");
      if (!res.ok) return;
      map.getSource(HEAT_SOURCE)?.setData(buildHeatFC(await res.json()));
      heatLoaded = true;
    } catch {
      // Leave the grid empty on failure; the toggle still works.
    }
  }

  function setMode(next) {
    if (next === mode || !map || !layerReady) return;
    mode = next;
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

  function emptyFC() {
    return { type: "FeatureCollection", features: [] };
  }

  // Marker fills come from VESSEL_COLORS (vibrant, single-sourced with the
  // legend); only the ink stroke is pulled from the design tokens.
  function palette() {
    const s = getComputedStyle(document.documentElement);
    return {
      ...VESSEL_COLORS,
      ink: s.getPropertyValue("--ink").trim(),
    };
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

  // Rasterize the ship arrow once per palette color into a canvas image and
  // register it with the map. Per-color icons let a single data-driven
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
      if (map.hasImage(id)) continue;
      const canvas = document.createElement("canvas");
      canvas.width = px;
      canvas.height = px;
      const ctx = canvas.getContext("2d");
      ctx.scale(px / 20, px / 20); // path authored in a 20x20 viewBox
      const path = new Path2D("M10 1 L17 18 L10 14 L3 18 Z");
      // The basemap renders at native saturation (no canvas filter), so the
      // fills are drawn at their true legend colors with no saturation
      // compensation.
      ctx.fillStyle = fill;
      ctx.fill(path);
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.strokeStyle = p.ink;
      ctx.stroke(path);
      map.addImage(id, ctx.getImageData(0, 0, px, px), { pixelRatio: ratio });
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

    if (!didFit && seen.size > 0) {
      const lats = [];
      const lons = [];
      for (const v of vessels) {
        if (!validLatLon(v.lat, v.lon)) continue;
        lats.push(v.lat);
        lons.push(v.lon);
      }
      if (lats.length) {
        lats.sort((a, b) => a - b);
        lons.sort((a, b) => a - b);
        // Frame the dense core of the fleet (5th-95th percentile) rather than
        // its full spread: the AIS feed is regionally clustered, so a handful
        // of near-outliers would otherwise pull the bounds wide and leave the
        // screen mostly empty water. Trimming to the core lets the initial fit
        // land on where the vessels actually are; the outliers still render as
        // markers, reachable by panning/zooming out.
        const q = 0.05;
        const b = new maplibregl.LngLatBounds(
          [quantile(lons, q), quantile(lats, q)],
          [quantile(lons, 1 - q), quantile(lats, 1 - q)],
        );
        // maxZoom 12 (was 9) lets a tight cluster fill the viewport instead of
        // being capped far out; fitBounds only reaches it when the core really
        // is that small, so a wider fleet still frames naturally.
        map.fitBounds(b, { padding: 48, maxZoom: 12, duration: 0 });
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
    selected = v;
    selectedMmsi = String(v.mmsi);
    pushData(); // highlight immediately, don't wait for the next DR tick
    track = null;
    trackLoading = true;
    try {
      // Same-origin site route, proxied to /api/ships/track/{mmsi} server-side
      // by +server.js. The browser never touches the private API surface.
      const res = await fetch(`/app/ships/track/${encodeURIComponent(v.mmsi)}`);
      if (res.ok) {
        const body = await res.json();
        track = body;
        drawTrack(body.track);
      }
    } catch {
      // Network blip: keep the panel open with snapshot fields, just no track.
    } finally {
      trackLoading = false;
    }
  }

  function closePanel() {
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

  onMount(() => {
    let cleanup = () => {};
    let destroyed = false;

    (async () => {
      maplibregl = (await import("maplibre-gl")).default;
      if (destroyed) return;

      map = new maplibregl.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
        center: [0, 30],
        zoom: 1.4,
        // Render the OSM/OpenMapTiles attribution as a compact "i" button
        // instead of letting the full credit line sprawl across the bottom of
        // the map (especially cramped on mobile). MapLibre opens this expanded
        // on first paint and only minimizes it once the user pans, so we also
        // collapse it up front below; tapping the button still reveals the
        // required credits.
        attributionControl: { compact: true },
      });
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
      mapContainer.addEventListener("click", (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      });
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

      map.on("load", () => {
        pal = palette();
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
            "fill-color": HEAT_FILL_COLOR,
            // High opacity keeps the beige/green basemap from bleeding through
            // and muting the ramp; the cells already sit on water/lanes so the
            // map underneath stays readable at the edges.
            "fill-opacity": 0.9,
            "fill-outline-color": "rgba(0, 0, 0, 0)",
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
          requestIdleCallback(() => loadHeat(), { timeout: 3000 });
        } else {
          setTimeout(() => loadHeat(), 1500);
        }
      });

      cleanup = () => {
        if (rafId) cancelAnimationFrame(rafId);
        fleet.clear();
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

<div class="map-wrap" class:heat-mode={mode === "heat"} class:panel-open={selected}>
  <div class="map" bind:this={mapContainer}></div>

  <nav class="map-chip" aria-label="Breadcrumb">
    <a class="chip-home" href="https://jomcgi.dev/"
      >jomcgi.dev<span class="chip-home-arrow" aria-hidden="true">↗</span></a
    >
    <span class="chip-sep">/</span>
    <span class="chip-name">ships</span>
    <span class="chip-sep chip-sep-live">/</span>
    <span class="chip-live">live</span>
    <span class="chip-dot" aria-hidden="true"></span>
  </nav>

  <div class="mode-toggle" role="group" aria-label="Map mode">
    <button class:active={mode === "vessels"} onclick={() => setMode("vessels")}>
      Vessels
    </button>
    <button class:active={mode === "heat"} onclick={() => setMode("heat")}>
      Heat
    </button>
  </div>

  {#if mode === "vessels"}
    <div class="legend">
      <p class="eyebrow legend-title">Vessel type</p>
      <div class="legend-grid">
        {#each LEGEND as item (item.key)}
          <button
            type="button"
            class="legend-item"
            class:is-off={!active.has(item.key)}
            style="background: {item.color}"
            aria-pressed={active.has(item.key)}
            onclick={() => toggleType(item.key)}
          >
            <span class="legend-label">{item.label}</span>
          </button>
        {/each}
      </div>
    </div>
  {:else}
    <div class="legend">
      <p class="eyebrow legend-title">Vessels / cell · 7d</p>
      <ul class="heat-scale">
        {#each HEAT_STOPS as s (s.at)}
          <li>
            <span class="heat-sw" style="background: {s.color}"></span>{s.label}
          </li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if selected}
    <aside class="panel">
      <button class="panel-close" onclick={closePanel} aria-label="Close vessel panel"
        >&times;</button
      >
      <h2 class="panel-name">
        {selected.name || selected.ship_name || `MMSI ${selected.mmsi}`}
      </h2>
      <dl class="panel-rows">
        <div><dt>MMSI</dt><dd>{selected.mmsi}</dd></div>
        <div><dt>Type</dt><dd>{shipTypeLabel(selected.ship_type)}</dd></div>
        <div><dt>Speed</dt><dd>{fmtSpeed(selected.speed)}</dd></div>
        <div><dt>Course</dt><dd>{fmtDeg(selected.course)}</dd></div>
        <div><dt>Heading</dt><dd>{fmtDeg(selected.heading)}</dd></div>
        <div>
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
    background: var(--cream);
  }

  .map {
    position: absolute;
    inset: 0;
  }

  /* No CSS filter on the basemap canvas: it renders at the OpenFreeMap style's
     native colors. Earlier revisions desaturated it toward the cream palette,
     but that read as dull next to the vibrant heatmap, and even a light tint
     wasn't worth the muting, so the markers and heat ramp now sit on the full
     basemap. */

  /* The bottom-right stack (zoom + the collapsed attribution "i") sits in the
     map's corner, where a device safe area can clip the controls offscreen:
     the home-indicator inset on the bottom, and on a notched device held in
     landscape the rounded-corner/notch inset on the right. Inset the whole
     corner past both so nothing hides behind them. Falls back to 0 on devices
     without a safe area. */
  .map :global(.maplibregl-ctrl-bottom-right) {
    bottom: env(safe-area-inset-bottom, 0);
    right: env(safe-area-inset-right, 0);
  }

  /* Drop the group container entirely so each +/- button is its own square
     that lifts off, rather than a button recessing into a bordered box. */
  .map :global(.maplibregl-ctrl-group) {
    border: none;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
    overflow: visible; /* don't clip the lifted button's shadow */
  }

  .map :global(.maplibregl-ctrl-group button) {
    border: 2px solid var(--ink);
    background: var(--paper);
    transition:
      transform 110ms ease,
      box-shadow 110ms ease;
  }

  /* maplibre stacks the +/- buttons flush; separate them so each lifts alone. */
  .map :global(.maplibregl-ctrl-group button + button) {
    margin-top: 6px;
  }

  /* Lift off and KEEP the shadow on hover/press/focus, like the homepage
     buttons. No collapse-to-flat on :active (that read as "falling in"). */
  .map :global(.maplibregl-ctrl-group button:hover),
  .map :global(.maplibregl-ctrl-group button:focus-visible),
  .map :global(.maplibregl-ctrl-group button:active) {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
    background: var(--paper);
    position: relative;
    z-index: 1;
  }

  /* Compact attribution: a single square "i" toggle that matches the zoom
     controls stacked above it, rather than MapLibre's default round white pill.
     The required OSM/OpenMapTiles credits expand on tap.

     The "i" button is `position: absolute; top: 0` inside this container, so the
     container has to stay tall enough to hold it: MapLibre's default min-height
     does that, but a previous `min-height: 0` collapsed the box to ~4px and the
     24px button then overflowed *below* it. Because this control is anchored to
     the map's bottom edge, that overflow fell off the bottom of the viewport and
     clipped the lower ~10px of the "i". Match the container to the button (24px,
     vertical padding zeroed for a clean square) so the button is fully held. */
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

  .map-chip {
    position: absolute;
    top: 16px;
    left: 16px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: var(--paper);
    border: 2px solid var(--ink);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  /* Vessels/Heat switch, top-right. The detail panel is shifted below it. */
  .mode-toggle {
    position: absolute;
    top: 16px;
    right: 16px;
    display: inline-flex;
    border: 2px solid var(--ink);
    background: var(--paper);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .mode-toggle button {
    padding: 8px 14px;
    background: var(--paper);
    border: none;
    color: var(--ink);
    cursor: pointer;
    transition: background 120ms ease;
  }

  .mode-toggle button + button {
    border-left: 2px solid var(--ink);
  }

  .mode-toggle button:hover {
    background: var(--cream);
  }

  .mode-toggle button.active {
    background: var(--ink);
    color: var(--paper);
  }

  .heat-scale {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 5px 14px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }

  .heat-scale li {
    display: flex;
    align-items: center;
    gap: 7px;
  }

  .heat-sw {
    width: 13px;
    height: 13px;
    border: 1.5px solid var(--ink);
    flex: none;
  }

  /* The home crumb is a real link out to the apex: underlined by default with
     a small click-through arrow, and on hover/focus it gets the CV's
     highlighter-marker treatment (bold + accent swipe) to read as "selected". */
  .chip-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    /* 2px (even at 2x DPR) + skip-ink: none renders a clean continuous rule;
       1.5px left a faint lighter streak and skip-ink notched the underline
       around the "." and the arrow. */
    text-decoration-thickness: 2px;
    text-decoration-skip-ink: none;
    text-underline-offset: 2px;
    padding: 0 2px;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
    transition:
      background 140ms ease,
      text-decoration-color 140ms ease;
  }

  .chip-home:hover,
  .chip-home:focus-visible {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
    text-decoration-color: var(--ink);
  }

  /* inline-block keeps the underline from running under the arrow glyph. */
  .chip-home-arrow {
    display: inline-block;
    margin-left: 2px;
    font-size: 0.85em;
    vertical-align: 0.05em;
    text-decoration: none;
  }

  .chip-sep {
    color: var(--ink-3);
  }

  .chip-dot {
    width: 9px;
    height: 9px;
    border-radius: 999px;
    background: var(--green);
    border: 1px solid var(--ink);
    animation: chip-pulse 1.6s ease-in-out infinite;
  }

  @keyframes chip-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.3;
    }
  }

  /* Hard rectangle (no card-hard: that rounds the corners and lifts on hover,
     which read as "clickable" on a non-interactive panel). */
  .legend {
    position: absolute;
    bottom: 16px;
    left: 16px;
    padding: 12px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .legend-title {
    margin-bottom: 10px;
  }

  .legend-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }

  /* Each box is a filter toggle: the box fill is the vessel-type color, with
     the label on a paper inset so it stays legible on any fill. Clicking
     toggles that type on the map. */
  .legend-item {
    display: flex;
    padding: 4px;
    border: 2px solid var(--ink);
    cursor: pointer;
    transition:
      transform 110ms ease,
      box-shadow 110ms ease,
      opacity 110ms ease;
  }

  .legend-label {
    width: 100%;
    padding: 3px 7px;
    background: var(--paper);
    border: 1px solid var(--ink);
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: var(--ink);
  }

  .legend-item:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
  }

  /* Stay lifted while pressed (no "fall in"); the shadow just tucks in a hair. */
  .legend-item:active {
    transform: translate(-1px, -1px);
    box-shadow: 1px 1px 0 var(--ink);
  }

  /* Filtered-out types dim but stay clickable to re-enable. */
  .legend-item.is-off {
    opacity: 0.4;
  }

  /* Hard rectangle to match the chip + legend (no card-hard: that rounds the
     corners and lifts on hover, which read as interactive on a display panel). */
  .panel {
    position: absolute;
    /* Clears the Vessels/Heat toggle (top-right). */
    top: 64px;
    right: 16px;
    width: 280px;
    max-width: calc(100% - 32px);
    padding: 18px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .panel-close {
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

  .panel-name {
    font-family: var(--serif);
    font-size: 28px;
    line-height: 1.05;
    margin: 2px 0 14px;
    word-break: break-word;
  }

  .panel-rows {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .panel-rows > div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    border-bottom: 1px dashed var(--rule-2);
    padding-bottom: 6px;
  }

  .panel-rows dt {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .panel-rows dd {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    text-align: right;
  }

  .panel-meta {
    margin-top: 12px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    letter-spacing: 0.04em;
  }

  @media (max-width: 640px) {
    /* Tighten the breadcrumb so the enlarged mode toggle has room beside it. */
    .map-chip {
      top: 12px;
      left: 12px;
      gap: 6px;
      padding: 7px 9px;
      font-size: 11px;
    }

    /* On a phone the chip and the (larger) mode toggle share the top row and
       collide right where "live" meets "Vessels". Drop the redundant "/ live"
       text: the pulsing green dot already signals live, and losing those
       characters gives the toggle clear room beside the chip. */
    .chip-sep-live,
    .chip-live {
      display: none;
    }

    /* The primary control on mobile: bigger buttons, bigger touch targets. */
    .mode-toggle {
      top: 12px;
      right: 12px;
      font-size: 13px;
    }

    .mode-toggle button {
      padding: 12px 20px;
    }

    /* Filters stay on screen (desktop hid them) and grow into easy-to-tap
       chips, anchored bottom-left clear of the zoom controls bottom-right. */
    .legend {
      bottom: 12px;
      left: 12px;
      padding: 12px 14px;
    }

    .legend-grid {
      gap: 10px;
    }

    .legend-label {
      padding: 6px 9px;
      font-size: 12px;
    }

    .heat-scale {
      font-size: 12px;
      gap: 8px 16px;
    }

    .heat-sw {
      width: 15px;
      height: 15px;
    }

    /* Reading a vessel takes over the lower screen, so drop the filters while
       the sheet is up rather than stacking the two bottom panels. */
    .panel-open .legend {
      display: none;
    }

    /* Vessel info becomes a full-bleed bottom sheet that fills more of the
       screen with larger, more legible type. */
    .panel {
      top: auto;
      bottom: 0;
      left: 0;
      right: 0;
      width: auto;
      max-width: none;
      max-height: 72vh;
      overflow-y: auto;
      padding: 22px 20px calc(22px + env(safe-area-inset-bottom));
      border-width: 2px 0 0;
    }

    .panel-close {
      top: 14px;
      right: 16px;
      font-size: 26px;
    }

    .panel-name {
      font-size: 32px;
    }

    .panel-rows {
      gap: 12px;
    }

    .panel-rows > div {
      padding-bottom: 10px;
    }

    .panel-rows dt {
      font-size: 11px;
    }

    .panel-rows dd {
      font-size: 14px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .chip-dot {
      animation: none;
    }
    .map :global(.maplibregl-ctrl-group button) {
      transition: none;
    }
    .legend-item {
      transition: none;
    }
  }
</style>
