<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import { dayColor } from "$lib/trips/trip.js";

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
  let map = null;
  let ready = false;

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

    (async () => {
      const maplibregl = (await import("maplibre-gl")).default;
      if (destroyed) return;

      const b = bounds();
      map = new maplibregl.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
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

      map.on("load", () => {
        days.forEach((day, i) => {
          const coords = day.points
            .filter((p) => p.lat != null && p.lng != null)
            .map((p) => [p.lng, p.lat]);
          if (coords.length < 2) return;
          const color = dayColor(i);
          const sourceId = `route-src-${i}`;
          map.addSource(sourceId, {
            type: "geojson",
            data: {
              type: "Feature",
              properties: {},
              geometry: { type: "LineString", coordinates: coords },
            },
          });
          // Wide transparent hit layer first (easier click/hover target).
          map.addLayer({
            id: `route-hit-${i}`,
            type: "line",
            source: sourceId,
            layout: { "line-join": "round", "line-cap": "round" },
            paint: { "line-color": color, "line-opacity": 0, "line-width": 18 },
          });
          map.addLayer({
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

          map.on("mouseenter", `route-hit-${i}`, () => {
            map.getCanvas().style.cursor = "pointer";
            onHover(i);
          });
          map.on("mouseleave", `route-hit-${i}`, () => {
            map.getCanvas().style.cursor = "";
            onHover(null);
          });
          map.on("click", `route-hit-${i}`, () => onDayClick(day.dayNumber));
        });

        // Start marker as a small circle layer (GeoJSON, not a DOM marker).
        const first = allPoints()[0];
        if (first) {
          map.addSource("trip-start", {
            type: "geojson",
            data: {
              type: "Feature",
              properties: {},
              geometry: { type: "Point", coordinates: [first.lng, first.lat] },
            },
          });
          map.addLayer({
            id: "trip-start",
            type: "circle",
            source: "trip-start",
            paint: {
              "circle-radius": 6,
              "circle-color": "#1a1a1a",
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 2,
            },
          });
        }

        ready = true;
        applyHover();
      });

      cleanup = () => {
        map?.remove();
        map = null;
        ready = false;
      };
    })();

    return () => {
      destroyed = true;
      cleanup();
    };
  });
</script>

<div class="map" bind:this={mapContainer}></div>

<style>
  .map {
    width: 100%;
    height: 100%;
    min-height: 240px;
  }
  .map :global(.maplibregl-ctrl-attrib-inner) {
    font-family: var(--mono);
    font-size: 10px;
  }
</style>
