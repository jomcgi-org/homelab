<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";

  // Single-day route map, a faithful port of the original React DayMap. The
  // basemap is the light Carto Positron style, drawn in its normal light form
  // (no CSS inversion), with a neo-brutalist high-contrast route on top. Layers,
  // in z-order: a thick ink `route-line` casing, a thin `route-color-accent`
  // (the real day colour) down its centre, and a wide transparent
  // `route-hit-area` for click-to-select. DOM markers mark the start, end and
  // the current photo (a day-colour square). A subtle terrain hillshade sits
  // under the basemap, its illumination direction relit from the photo-time sun.
  //
  // `currentCoords` ([lng, lat] or null) is the current photo's location with the
  // page's GPS interpolation already applied, so the square marker tracks photos
  // that lack their own fix. `sunPosition` ({ altitude, azimuth } in radians)
  // drives the hillshade relighting. `onLocationClick(takenAtIso)` fires with the
  // nearest route point's capture time when the route is clicked; the page maps
  // that to the closest photo.
  let {
    points = [],
    dayColor = "#2563eb",
    height = "280px",
    currentCoords = null,
    sunPosition = null,
    onLocationClick,
  } = $props();

  const BASEMAP_STYLE =
    "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

  let mapContainer;
  let map = null;
  let maplibre = null;
  let photoMarker = null;
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

  // Drop points within ~20m of the previous one (multiple GPS sources stack
  // duplicates that thicken the rendered line).
  function dedupePoints(pts) {
    if (!pts.length) return [];
    const result = [pts[0]];
    for (let i = 1; i < pts.length; i++) {
      const prev = result[result.length - 1];
      const curr = pts[i];
      const dlat = Math.abs(curr.lat - prev.lat);
      const dlng = Math.abs(curr.lng - prev.lng);
      if (dlat > 0.0002 || dlng > 0.0002) result.push(curr);
    }
    return result;
  }

  // --- hillshade relighting from the sun ---
  // SunCalc azimuth is radians from south, positive west; MapLibre's
  // illumination-direction is degrees from north.
  function illuminationDirection(sun) {
    if (!sun) return 315;
    const azimuthDeg = (sun.azimuth * 180) / Math.PI + 180;
    return ((azimuthDeg % 360) + 360) % 360;
  }

  // Subtle, standard (non-inverted) hillshade: shadows dark, highlights light.
  // The sun still sets the illumination direction (azimuth) and the relief
  // exaggeration (terrain reads stronger at low sun); the colours are fixed
  // normals so the shading sits quietly under the light Positron basemap.
  function sunIntensity(sun) {
    const shadowColor = "#5a5a5a";
    const highlightColor = "#ffffff";
    if (!sun) {
      return { exaggeration: 0.8, shadowColor, highlightColor };
    }
    const altitudeDeg = (sun.altitude * 180) / Math.PI;
    const lerp = (a, b, t) => a + (b - a) * Math.max(0, Math.min(1, t));
    let exaggeration;
    if (altitudeDeg < -6) {
      exaggeration = lerp(0.2, 0.5, (altitudeDeg + 12) / 6);
    } else if (altitudeDeg < 5) {
      exaggeration = lerp(0.5, 1.0, (altitudeDeg + 6) / 11);
    } else {
      exaggeration = 1.0;
    }
    return { exaggeration, shadowColor, highlightColor };
  }

  // Reposition + recentre on the current photo. The square marker eases over
  // 300ms (ease-out-cubic), and the camera eases to zoom 10, matching the
  // original. Guarded on mapReady so it no-ops until the marker can be created.
  $effect(() => {
    const coords = currentCoords;
    if (!mapReady || !map || !maplibre) return;
    if (coords) {
      if (photoMarker) {
        const start = photoMarker.getLngLat();
        const startTime = performance.now();
        const duration = 300;
        const animate = (now) => {
          const t = Math.min((now - startTime) / duration, 1);
          const eased = 1 - Math.pow(1 - t, 3);
          const lng = start.lng + (coords[0] - start.lng) * eased;
          const lat = start.lat + (coords[1] - start.lat) * eased;
          photoMarker.setLngLat([lng, lat]);
          if (t < 1) requestAnimationFrame(animate);
        };
        requestAnimationFrame(animate);
      } else {
        const el = document.createElement("div");
        // Day-colour square with a chunky ink border (neo-brutalist).
        el.style.cssText = `width:24px;height:24px;background:${dayColor};border:3px solid #1a1a1a;cursor:pointer;`;
        photoMarker = new maplibre.Marker({ element: el })
          .setLngLat(coords)
          .addTo(map);
      }
      map.easeTo({
        center: coords,
        zoom: 10,
        duration: 300,
        easing: (t) => 1 - Math.pow(1 - t, 3),
      });
    } else if (photoMarker) {
      photoMarker.remove();
      photoMarker = null;
    }
  });

  // Relight the hillshade when the sun position changes.
  $effect(() => {
    const sun = sunPosition;
    if (!mapReady || !map || !map.getLayer("hillshade")) return;
    const intensity = sunIntensity(sun);
    map.setPaintProperty(
      "hillshade",
      "hillshade-illumination-direction",
      illuminationDirection(sun),
    );
    map.setPaintProperty(
      "hillshade",
      "hillshade-exaggeration",
      intensity.exaggeration,
    );
    map.setPaintProperty(
      "hillshade",
      "hillshade-shadow-color",
      intensity.shadowColor,
    );
    map.setPaintProperty(
      "hillshade",
      "hillshade-highlight-color",
      intensity.highlightColor,
    );
  });

  onMount(() => {
    let destroyed = false;
    let cleanup = () => {};

    (async () => {
      maplibre = (await import("maplibre-gl")).default;
      if (destroyed) return;

      const b = bounds();
      map = new maplibre.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
        ...(b
          ? { bounds: b, fitBoundsOptions: { padding: 50 } }
          : { center: [0, 30], zoom: 1.4 }),
        dragRotate: false,
        attributionControl: false,
      });
      map.addControl(
        new maplibre.NavigationControl({ showCompass: false }),
        "top-right",
      );

      map.on("load", () => {
        // Terrain hillshade, lit from the photo-time sun, under the basemap.
        map.addSource("terrain-dem", {
          type: "raster-dem",
          tiles: [
            "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
          ],
          encoding: "terrarium",
          tileSize: 256,
          maxzoom: 15,
        });
        const firstSymbol = map
          .getStyle()
          .layers.find((l) => l.type === "symbol");
        const intensity = sunIntensity(sunPosition);
        map.addLayer(
          {
            id: "hillshade",
            type: "hillshade",
            source: "terrain-dem",
            minzoom: 0,
            maxzoom: 22,
            paint: {
              "hillshade-exaggeration": intensity.exaggeration,
              "hillshade-shadow-color": intensity.shadowColor,
              "hillshade-highlight-color": intensity.highlightColor,
              "hillshade-accent-color": "transparent",
              "hillshade-illumination-direction":
                illuminationDirection(sunPosition),
            },
          },
          firstSymbol?.id,
        );

        const routePoints = dedupePoints(geoPoints());
        const coordinates = routePoints.map((p) => [p.lng, p.lat]);
        if (coordinates.length >= 2) {
          map.addSource("route", {
            type: "geojson",
            data: {
              type: "Feature",
              geometry: { type: "LineString", coordinates },
            },
          });
          // Thick ink casing: reads boldly on the light Positron basemap.
          map.addLayer({
            id: "route-line",
            type: "line",
            source: "route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
              "line-color": "#1a1a1a",
              "line-width": 6,
              "line-opacity": 1,
            },
          });
          // Thin real-day-colour core down the centre of the ink casing.
          map.addLayer({
            id: "route-color-accent",
            type: "line",
            source: "route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
              "line-color": dayColor,
              "line-width": 2.5,
              "line-opacity": 1,
            },
          });
          // Wide transparent hit area for easier clicking.
          map.addLayer({
            id: "route-hit-area",
            type: "line",
            source: "route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
              "line-color": "transparent",
              "line-width": 20,
              "line-opacity": 0,
            },
          });

          if (onLocationClick) {
            map.on("mouseenter", "route-hit-area", () => {
              map.getCanvas().style.cursor = "pointer";
            });
            map.on("mouseleave", "route-hit-area", () => {
              map.getCanvas().style.cursor = "";
            });
            map.on("click", "route-hit-area", (e) => {
              const { lng: clickLng, lat: clickLat } = e.lngLat;
              let minDist = Infinity;
              let closest = null;
              for (const p of points) {
                if (p.lat == null || p.lng == null || !p.taken_at) continue;
                const dist =
                  Math.pow(p.lng - clickLng, 2) + Math.pow(p.lat - clickLat, 2);
                if (dist < minDist) {
                  minDist = dist;
                  closest = p;
                }
              }
              if (closest?.taken_at) onLocationClick(closest.taken_at);
            });
          }
        }

        // Start / end DOM markers: start = solid ink dot with a white ring,
        // end = white dot with an ink ring. The square current-photo marker is
        // created by the $effect.
        const pts = geoPoints();
        if (pts.length > 0) {
          const startEl = document.createElement("div");
          startEl.style.cssText =
            "width:16px;height:16px;background:#1a1a1a;border:3px solid #ffffff;border-radius:50%;"; /* nosemgrep: svelte-hardcoded-color-in-style */
          new maplibre.Marker({ element: startEl })
            .setLngLat([pts[0].lng, pts[0].lat])
            .addTo(map);

          const last = pts[pts.length - 1];
          const distance = Math.sqrt(
            Math.pow(last.lng - pts[0].lng, 2) +
              Math.pow(last.lat - pts[0].lat, 2),
          );
          if (distance > 0.01) {
            const endEl = document.createElement("div");
            endEl.style.cssText =
              "width:14px;height:14px;background:#ffffff;border:3px solid #1a1a1a;border-radius:50%;"; /* nosemgrep: svelte-hardcoded-color-in-style */
            new maplibre.Marker({ element: endEl })
              .setLngLat([last.lng, last.lat])
              .addTo(map);
          }
        }

        mapReady = true;
      });

      cleanup = () => {
        photoMarker?.remove();
        photoMarker = null;
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

<div class="map" style={`height:${height}`} bind:this={mapContainer}></div>

<style>
  /* Light Positron basemap framed as a neo-brutalist card: chunky ink border
     and a hard offset drop-shadow (no blur). box-sizing keeps the framed box at
     its given height so the border does not change the outer dimensions. */
  .map {
    width: 100%;
    box-sizing: border-box;
    overflow: hidden;
    border: 3px solid #1a1a1a;
    box-shadow: 5px 5px 0 0 #1a1a1a;
  }
</style>
