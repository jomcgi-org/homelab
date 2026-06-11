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

  function emptyFC() {
    return { type: "FeatureCollection", features: [] };
  }

  // Resolve palette from the design tokens so colors stay single-sourced in
  // design-system.css rather than duplicated as hex literals here.
  function palette() {
    const s = getComputedStyle(document.documentElement);
    const g = (name) => s.getPropertyValue(name).trim();
    return {
      passenger: g("--blue"),
      cargo: g("--teal"),
      tanker: g("--coral"),
      hsc: g("--accent"),
      special: g("--green"),
      unknown: g("--ink-3"),
      ink: g("--ink"),
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

  // Reset fleet state to snapshot truth whenever a new snapshot arrives, then
  // repaint. `vessels` is a fresh array on every invalidateAll.
  function syncVessels() {
    if (!map || !layerReady) return;

    const seen = new Set();
    for (const v of vessels) {
      if (v.lat == null || v.lon == null) continue;
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
      const b = new maplibregl.LngLatBounds();
      let any = false;
      for (const v of vessels) {
        if (v.lat == null || v.lon == null) continue;
        b.extend([v.lon, v.lat]);
        any = true;
      }
      if (any) {
        map.fitBounds(b, { padding: 64, maxZoom: 9, duration: 0 });
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
        attributionControl: true,
      });
      map.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );

      map.on("load", () => {
        pal = palette();
        registerIcons(pal);

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

<div class="map-wrap">
  <div class="map" bind:this={mapContainer}></div>

  <div class="map-chip">
    <span class="chip-name">Ships</span>
    <span class="chip-sep">/</span>
    <span class="chip-live">live</span>
    <span class="chip-dot" aria-hidden="true"></span>
  </div>

  <div class="legend card-hard">
    <p class="eyebrow legend-title">Vessel type</p>
    <ul class="legend-list">
      <li><span class="sw sw-passenger"></span>Passenger</li>
      <li><span class="sw sw-cargo"></span>Cargo</li>
      <li><span class="sw sw-tanker"></span>Tanker</li>
      <li><span class="sw sw-hsc"></span>High-speed</li>
      <li><span class="sw sw-special"></span>Special</li>
      <li><span class="sw sw-unknown"></span>Other</li>
    </ul>
  </div>

  {#if selected}
    <aside class="panel card-hard">
      <button class="panel-close" onclick={closePanel} aria-label="Close vessel panel"
        >&times;</button
      >
      <p class="eyebrow">Vessel</p>
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

  /* Push the basemap toward the warm cream/ink palette without restyling
     every layer. Kept subtle so markers and chrome stay legible. */
  .map :global(.maplibregl-canvas) {
    filter: grayscale(0.4) sepia(0.12) brightness(1.02) contrast(0.96);
  }

  .map :global(.maplibregl-ctrl-group) {
    border: 2px solid var(--ink);
    border-radius: 0;
    box-shadow: var(--shadow-hard-sm);
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
    box-shadow: var(--shadow-hard);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
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

  .legend {
    position: absolute;
    bottom: 16px;
    left: 16px;
    padding: 12px 14px;
    background: var(--paper);
  }

  .legend-title {
    margin-bottom: 8px;
  }

  .legend-list {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 4px 14px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.04em;
  }

  .legend-list li {
    display: flex;
    align-items: center;
    gap: 7px;
  }

  .sw {
    width: 11px;
    height: 11px;
    border: 1.5px solid var(--ink);
    flex: none;
  }

  .sw-passenger {
    background: var(--blue);
  }
  .sw-cargo {
    background: var(--teal);
  }
  .sw-tanker {
    background: var(--coral);
  }
  .sw-hsc {
    background: var(--accent);
  }
  .sw-special {
    background: var(--green);
  }
  .sw-unknown {
    background: var(--ink-3);
  }

  .panel {
    position: absolute;
    top: 16px;
    right: 16px;
    width: 280px;
    max-width: calc(100% - 32px);
    padding: 18px;
    background: var(--paper);
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
    .legend {
      display: none;
    }
    .panel {
      top: auto;
      bottom: 16px;
      left: 16px;
      right: 16px;
      width: auto;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .chip-dot {
      animation: none;
    }
  }
</style>
