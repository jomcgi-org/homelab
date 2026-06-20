<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";

  // Single-day route map: the day's GPS line plus a circle layer marking each
  // photo location. Interactive (pan/zoom). Follows the ShipsMap pattern
  // (lazy maplibre import, GeoJSON sources + layers).
  //
  // `current` is the scrubber's selected photo index: the matching marker is
  // highlighted (a larger ring on the `day-photo-current` layer) and the map
  // eases to its coordinates. Clicking any marker calls onPhotoClick(i) so the
  // page can set `current`.
  //
  // TODO(trips): the old React DayMap drove a per-photo marker + terrain
  // hillshade lit from the sun position at the photo's timestamp. That solar /
  // bearing panel is dropped in this port; photos are shown as static markers.
  let {
    points = [],
    photos = [],
    dayColor = "#2563eb",
    current = 0,
    onPhotoClick,
  } = $props();

  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";

  let mapContainer;
  let map = null;
  let mapReady = $state(false);

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

  // Coordinates of the photo at index `i`, or null if it has no GPS fix.
  function photoCoords(i) {
    const p = photos[i];
    if (!p || p.lat == null || p.lng == null) return null;
    return [p.lng, p.lat];
  }

  // Keep the highlight layer + camera in sync with the scrubber's `current`.
  // Guarded on mapReady so it no-ops until the sources/layers exist.
  $effect(() => {
    const i = current;
    if (!mapReady || !map) return;
    const coords = photoCoords(i);
    const src = map.getSource("day-photo-current");
    if (src) {
      src.setData({
        type: "FeatureCollection",
        features: coords
          ? [
              {
                type: "Feature",
                properties: { photoIndex: i },
                geometry: { type: "Point", coordinates: coords },
              },
            ]
          : [],
      });
    }
    if (coords) map.easeTo({ center: coords, duration: 500 });
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

        // Carry each photo's index in the `photos` array so a marker click can
        // set that exact photo as the scrubber's current index. `photos` is the
        // same array the scrubber/elevation use, so the indices line up.
        const photoFeatures = photos
          .map((p, i) => ({ p, i }))
          .filter(({ p }) => p.lat != null && p.lng != null)
          .map(({ p, i }) => ({
            type: "Feature",
            properties: { photoIndex: i },
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

          // Highlight layer for the current photo: a larger ring drawn on top of
          // the base markers. Driven by the $effect above via its own source.
          map.addSource("day-photo-current", {
            type: "geojson",
            data: { type: "FeatureCollection", features: [] },
          });
          map.addLayer({
            id: "day-photo-current",
            type: "circle",
            source: "day-photo-current",
            paint: {
              "circle-radius": 9,
              "circle-color": dayColor,
              "circle-stroke-color": "#ffffff",
              "circle-stroke-width": 3,
            },
          });

          map.on("click", "day-photos", (e) => {
            const i = e.features?.[0]?.properties?.photoIndex;
            if (i != null) onPhotoClick?.(i);
          });
          map.on("mouseenter", "day-photos", () => {
            map.getCanvas().style.cursor = "pointer";
          });
          map.on("mouseleave", "day-photos", () => {
            map.getCanvas().style.cursor = "";
          });
        }

        // Signal the $effect that sources/layers exist; it then renders the
        // initial highlight + centers on the current photo.
        mapReady = true;
      });

      cleanup = () => {
        map?.remove();
        map = null;
        mapReady = false;
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
