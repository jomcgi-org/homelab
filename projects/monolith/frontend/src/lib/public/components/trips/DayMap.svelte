<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";

  // Single-day route map: the day's GPS line plus a circle layer marking each
  // photo location. Interactive (pan/zoom). Follows the ShipsMap pattern
  // (lazy maplibre import, GeoJSON sources + layers).
  //
  // TODO(trips): the old React DayMap drove a per-photo marker + terrain
  // hillshade lit from the sun position at the photo's timestamp. That solar /
  // bearing panel is dropped in this port; photos are shown as static markers.
  let { points = [], photos = [], dayColor = "#2563eb" } = $props();

  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  let mapContainer;
  let map = null;

  function geoPoints() {
    return points.filter((p) => p.lat != null && p.lng != null);
  }

  function bounds() {
    const pts = geoPoints();
    if (!pts.length) return null;
    const lats = pts.map((p) => p.lat);
    const lngs = pts.map((p) => p.lng);
    return [
      [Math.min(...lngs), Math.min(...lats)],
      [Math.max(...lngs), Math.max(...lats)],
    ];
  }

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
        ...(b ? { bounds: b, fitBoundsOptions: { padding: 50 } } : { center: [0, 30], zoom: 1.4 }),
        attributionControl: { compact: true },
        dragRotate: false,
        touchZoomRotate: false,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

      map.on("load", () => {
        const coords = geoPoints().map((p) => [p.lng, p.lat]);
        if (coords.length >= 2) {
          map.addSource("day-route", {
            type: "geojson",
            data: {
              type: "Feature",
              properties: {},
              geometry: { type: "LineString", coordinates: coords },
            },
          });
          map.addLayer({
            id: "day-route",
            type: "line",
            source: "day-route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: { "line-color": dayColor, "line-width": 4, "line-opacity": 1 },
          });
        }

        const photoFeatures = photos
          .filter((p) => p.lat != null && p.lng != null)
          .map((p) => ({
            type: "Feature",
            properties: {},
            geometry: { type: "Point", coordinates: [p.lng, p.lat] },
          }));
        if (photoFeatures.length) {
          map.addSource("day-photos", {
            type: "geojson",
            data: { type: "FeatureCollection", features: photoFeatures },
          });
          map.addLayer({
            id: "day-photos",
            type: "circle",
            source: "day-photos",
            paint: {
              "circle-radius": 5,
              "circle-color": dayColor,
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 2,
            },
          });
        }
      });

      cleanup = () => {
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

<div class="map" bind:this={mapContainer}></div>

<style>
  .map {
    width: 100%;
    height: 100%;
    min-height: 200px;
  }
  .map :global(.maplibregl-ctrl-attrib-inner) {
    font-family: var(--mono);
    font-size: 10px;
  }
</style>
