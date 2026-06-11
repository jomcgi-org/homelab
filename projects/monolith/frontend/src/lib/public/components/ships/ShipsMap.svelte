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

  let maplibregl; // loaded lazily in onMount (browser-only module)
  let mapContainer; // bound <div>
  let map = null;
  let pal = null; // design-token palette, resolved once the map is up
  let rafId = null;
  let didFit = false;

  // mmsi -> { marker, el, onClick, fixLat, fixLon, fixTimeMs, speed, course }
  const markers = new Map();

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

  // ITU AIS ship_type bands mapped onto the palette.
  function shipBandColor(type, p) {
    if (type == null) return p.unknown;
    if (type >= 60 && type <= 69) return p.passenger;
    if (type >= 70 && type <= 79) return p.cargo;
    if (type >= 80 && type <= 89) return p.tanker;
    if (type >= 40 && type <= 49) return p.hsc;
    if (type >= 50 && type <= 59) return p.special;
    return p.unknown;
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

  function makeMarkerEl() {
    const el = document.createElement("div");
    el.className = "ship-marker";
    // fill = currentColor (set per-vessel via el.style.color); stroke pulls
    // the ink token straight from the cascade so it is not hardcoded here.
    el.innerHTML =
      '<svg viewBox="0 0 20 20" width="22" height="22" aria-hidden="true">' +
      '<path d="M10 1 L17 18 L10 14 L3 18 Z" fill="currentColor" ' +
      'style="stroke: var(--ink); stroke-width: 1.5; stroke-linejoin: round;" />' +
      "</svg>";
    return el;
  }

  function syncMarkers() {
    if (!map || !maplibregl) return;
    if (!pal) pal = palette();

    const seen = new Set();
    for (const v of vessels) {
      if (v.lat == null || v.lon == null) continue;
      const id = String(v.mmsi);
      seen.add(id);
      const color = shipBandColor(v.ship_type, pal);
      const rot = rotationOf(v);

      let entry = markers.get(id);
      if (!entry) {
        const el = makeMarkerEl();
        const marker = new maplibregl.Marker({
          element: el,
          rotation: rot,
          rotationAlignment: "map",
        })
          .setLngLat([v.lon, v.lat])
          .addTo(map);
        entry = { marker, el, onClick: null };
        markers.set(id, entry);
      } else {
        entry.marker.setRotation(rot);
      }
      entry.el.style.color = color;

      // Rebind the click handler so it always closes over the latest row.
      if (entry.onClick) entry.el.removeEventListener("click", entry.onClick);
      entry.onClick = (e) => {
        e.stopPropagation();
        selectVessel(v);
      };
      entry.el.addEventListener("click", entry.onClick);

      // Reset the dead-reckoning fix to snapshot truth.
      entry.fixLat = v.lat;
      entry.fixLon = v.lon;
      entry.fixTimeMs = fixTimeMs(v);
      entry.speed = v.speed;
      entry.course = v.course;
    }

    // Drop vessels that fell out of the snapshot.
    for (const [id, entry] of markers) {
      if (seen.has(id)) continue;
      if (entry.onClick) entry.el.removeEventListener("click", entry.onClick);
      entry.marker.remove();
      markers.delete(id);
    }

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

  function startLoop() {
    const step = () => {
      const now = Date.now();
      for (const entry of markers.values()) {
        const elapsed = Math.min(
          (now - entry.fixTimeMs) / 1000,
          MAX_DR_SECONDS,
        );
        const pos = deadReckon(
          {
            lat: entry.fixLat,
            lon: entry.fixLon,
            speed: entry.speed,
            course: entry.course,
          },
          elapsed,
        );
        entry.marker.setLngLat([pos.lon, pos.lat]);
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
    track = null;
    trackLoading = true;
    try {
      // Same-origin site route, proxied to /api/ships/track/{mmsi} server-side
      // by +server.js. The browser never touches the private API surface.
      const res = await fetch(
        `/app/ships/track/${encodeURIComponent(v.mmsi)}`,
      );
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
    track = null;
    const src = map?.getSource("ship-track");
    if (src) src.setData(emptyFC());
  }

  // Highlight the selected marker. Re-runs when `selected` changes.
  $effect(() => {
    const id = selected ? String(selected.mmsi) : null;
    for (const [mmsi, entry] of markers) {
      entry.el.classList.toggle("is-selected", mmsi === id);
    }
  });

  // Reset markers to snapshot truth whenever a new snapshot arrives. `vessels`
  // is a fresh array on every invalidateAll, so this fires on each live update.
  $effect(() => {
    void vessels;
    if (map) syncMarkers();
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
        syncMarkers();
        startLoop();
      });

      cleanup = () => {
        if (rafId) cancelAnimationFrame(rafId);
        for (const entry of markers.values()) {
          if (entry.onClick) entry.el.removeEventListener("click", entry.onClick);
          entry.marker.remove();
        }
        markers.clear();
        map?.remove();
        map = null;
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

  :global(.ship-marker) {
    cursor: pointer;
    line-height: 0;
    filter: drop-shadow(1px 1px 0 var(--ink));
    transition: transform 120ms ease;
  }

  :global(.ship-marker:hover) {
    transform: scale(1.25);
  }

  :global(.ship-marker.is-selected) {
    transform: scale(1.5);
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
    :global(.ship-marker) {
      transition: none;
    }
  }
</style>
