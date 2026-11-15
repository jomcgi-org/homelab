<script>
  import { onMount } from "svelte";
  import RoutePreview from "./RoutePreview.svelte";
  import { observeMapRequests } from "$lib/trips/map-requests.js";
  import "maplibre-gl/dist/maplibre-gl.css";
  import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
  import { dayColor } from "$lib/trips/trip.js";
  import {
    mapPalette,
    themeBasemap,
    watchMapScheme,
  } from "$lib/trips/map-theme.js";

  // Summary route map: one GPU GeoJSON line layer per day, coloured by day.
  // Clicking a day's line navigates to that day; hovering dims the others. A
  // static overview (pan/zoom disabled) so it never steals page scroll. Follows
  // the ShipsMap pattern: maplibre is imported lazily in onMount (browser only)
  // and routes are GeoJSON sources + layers, not DOM markers.
  let {
    days = [],
    hoveredDay = null,
    onHover = () => {},
    onDayClick = () => {},
  } = $props();

  // OpenFreeMap (no API key), the same basemap the ships map uses.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  let mapContainer;
  let map = $state.raw(null);
  let ready = false;
  let painted = $state(false);
  let failed = $state(false);
  let contextLost = $state(false);
  let retryRequested = false;
  let blocked = false;
  let requestObserver;

  function retryMap() {
    // setStyle during context loss would be overwritten by MapLibre's saved
    // style on restoration. Keep Retry visible and defer the requested reload.
    if (contextLost) {
      retryRequested = true;
      return;
    }
    retryRequested = false;
    requestObserver?.reset();
    blocked = false;
    painted = false;
    failed = true;
    ready = false;
    try {
      map.setStyle(BASEMAP_STYLE, { diff: false });
    } catch {
      blocked = true;
    }
  }

  function applyScheme() {
    if (!map || !ready) return;
    const palette = mapPalette(mapContainer);
    themeBasemap(map, palette);
    if (map.getLayer("trip-start")) {
      map.setPaintProperty("trip-start", "circle-color", palette.ink);
      map.setPaintProperty("trip-start", "circle-stroke-color", palette.sheet);
    }
  }

  function allPoints() {
    return days
      .flatMap((d) => d.points)
      .filter((p) => p.lat != null && p.lng != null);
  }

  function bounds() {
    const pts = allPoints();
    if (!pts.length) return null;
    const lats = pts.map((p) => p.lat);
    const lngs = pts.map((p) => p.lng);
    return [
      [Math.min(...lngs), Math.min(...lats)],
      [Math.max(...lngs), Math.max(...lats)],
    ];
  }

  function applyHover() {
    if (!map || !ready) return;
    days.forEach((_, i) => {
      const id = `route-${i}`;
      if (!map.getLayer(id)) return;
      const active = hoveredDay === null || hoveredDay === i;
      map.setPaintProperty(id, "line-opacity", active ? 1 : 0.15);
      map.setPaintProperty(id, "line-width", hoveredDay === i ? 5 : 3.5);
    });
  }

  $effect(() => {
    void hoveredDay;
    applyHover();
  });

  onMount(() => {
    let destroyed = false;
    let cleanup = () => {};
    const unwatch = watchMapScheme(applyScheme);

    (async () => {
      const maplibregl = await import("maplibre-gl");
      if (destroyed) return;
      // The package-relative default is absent from Vite's emitted chunks.
      maplibregl.setWorkerUrl(workerUrl);

      const markFailed = () => {
        blocked = true;
        failed = true;
        painted = false;
      };
      requestObserver = observeMapRequests(maplibregl, {
        onFailure: markFailed,
      });
      const b = bounds();
      map = new maplibregl.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
        transformRequest: requestObserver.transformRequest,
        ...(b
          ? { bounds: b, fitBoundsOptions: { padding: 40 } }
          : { center: [0, 30], zoom: 1.4 }),
        attributionControl: { compact: true },
        scrollZoom: false,
        dragPan: false,
        dragRotate: false,
        doubleClickZoom: false,
        touchZoomRotate: false,
        keyboard: false,
      });

      let sourceIds = [];
      map.on("error", markFailed);
      map.on("webglcontextlost", () => {
        contextLost = true;
        failed = true;
        painted = false;
        ready = false;
        requestObserver.reset();
      });
      map.on("webglcontextrestored", () => {
        contextLost = false;
        if (retryRequested) retryMap();
      });
      map.on("render", () => {
        if (
          contextLost ||
          blocked ||
          !ready ||
          !sourceIds.every((id) => map.getSource(id) && map.isSourceLoaded(id))
        )
          return;
        if (painted) return;
        failed = false;
        painted = true;
      });

      map.on("style.load", () => {
        if (contextLost) return;
        // Context restoration replays a serialized style with our sources and
        // layers already present. Reuse them instead of adding duplicate IDs.
        const addSource = (id, source) => {
          if (!map.getSource(id)) map.addSource(id, source);
        };
        const addLayer = (layer, before) => {
          if (!map.getLayer(layer.id)) map.addLayer(layer, before);
        };
        painted = false;
        ready = false;
        sourceIds = Object.keys(map.getStyle().sources);
        days.forEach((day, i) => {
          const coords = day.points
            .filter((p) => p.lat != null && p.lng != null)
            .map((p) => [p.lng, p.lat]);
          if (coords.length < 2) return;
          const color = dayColor(i);
          const sourceId = `route-src-${i}`;
          addSource(sourceId, {
            type: "geojson",
            data: {
              type: "Feature",
              properties: {},
              geometry: { type: "LineString", coordinates: coords },
            },
          });
          sourceIds.push(sourceId);
          // Wide transparent hit layer first (easier click/hover target).
          addLayer({
            id: `route-hit-${i}`,
            type: "line",
            source: sourceId,
            layout: { "line-join": "round", "line-cap": "round" },
            paint: { "line-color": color, "line-opacity": 0, "line-width": 18 },
          });
          addLayer({
            id: `route-${i}`,
            type: "line",
            source: sourceId,
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
              "line-color": color,
              "line-width": 3.5,
              "line-opacity": 1,
            },
          });
        });

        // Start marker as a small circle layer (GeoJSON, not a DOM marker).
        const first = allPoints()[0];
        if (first) {
          addSource("trip-start", {
            type: "geojson",
            data: {
              type: "Feature",
              properties: {},
              geometry: { type: "Point", coordinates: [first.lng, first.lat] },
            },
          });
          sourceIds.push("trip-start");
          addLayer({
            id: "trip-start",
            type: "circle",
            source: "trip-start",
            paint: {
              "circle-radius": 6,
              "circle-color": mapPalette(mapContainer).ink,
              "circle-stroke-color": mapPalette(mapContainer).sheet,
              "circle-stroke-width": 1,
            },
          });
        }

        ready = true;
        applyScheme();
        applyHover();
      });

      // Bind once: a retry replaces the style and sources on the same map.
      days.forEach((day, i) => {
        map.on("mouseenter", `route-hit-${i}`, () => {
          if (!painted) return;
          map.getCanvas().style.cursor = "pointer";
          onHover(i);
        });
        map.on("mouseleave", `route-hit-${i}`, () => {
          if (!painted) return;
          map.getCanvas().style.cursor = "";
          onHover(null);
        });
        map.on("click", `route-hit-${i}`, () => {
          if (painted) onDayClick(day.dayNumber);
        });
      });

      cleanup = () => {
        requestObserver?.dispose();
        map?.remove();
        map = null;
        ready = false;
      };
    })().catch(() => {
      requestObserver?.dispose();
      if (!destroyed) {
        failed = true;
        painted = false;
      }
    });

    return () => {
      destroyed = true;
      unwatch();
      cleanup();
    };
  });
</script>

<div class="map-wrap">
  {#if !painted}<RoutePreview
      {days}
      {failed}
      waitingForContext={contextLost}
      onRetry={map ? retryMap : undefined}
    />
  {:else if !allPoints().length}<p class="map-summary">
      No GPS points for this route.
    </p>{/if}
  <div class="map" bind:this={mapContainer}></div>
</div>

<style>
  .map-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 240px;
  }
  .map {
    width: 100%;
    height: 100%;
    min-height: 240px;
  }
  .map-summary {
    position: absolute;
    left: 0.75rem;
    bottom: 0.5rem;
    z-index: 1;
    margin: 0;
    background: var(--sheet);
    color: var(--ink);
    font: 0.7rem/1.5 var(--font-code);
    pointer-events: none;
  }
  .map :global(.maplibregl-ctrl-attrib-inner) {
    font-family: var(--font-code);
    font-size: 10px;
  }
</style>
