<script>
  import { onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";

  // Single-day route map, a faithful port of the original React DayMap. The
  // whole container is CSS `invert(1)`ed so the light Carto Positron basemap
  // reads as a dark map: every colour drawn into it is pre-inverted so it shows
  // correctly after the filter (a black line draws as white, the day colour is
  // inverted, etc). Layers, in z-order: a thick `route-line` (black -> white),
  // a thin `route-color-accent` (inverted day colour) down its centre, and a
  // wide transparent `route-hit-area` for click-to-select. DOM markers mark the
  // start, end and the current photo (a square). A sun-lit terrain hillshade
  // sits under the basemap, relit from the photo-time sun azimuth/altitude.
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

  // Invert a #rrggbb so it displays as the intended colour after the container's
  // CSS invert(1). Returns an rgb() string.
  function invertColor(hex) {
    const r = 255 - parseInt(hex.slice(1, 3), 16);
    const g = 255 - parseInt(hex.slice(3, 5), 16);
    const b = 255 - parseInt(hex.slice(5, 7), 16);
    return `rgb(${r}, ${g}, ${b})`;
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

  function sunIntensity(sun) {
    if (!sun) {
      return {
        exaggeration: 0.8,
        shadowColor: "#cccccc",
        highlightColor: "#000000",
      };
    }
    const altitudeDeg = (sun.altitude * 180) / Math.PI;
    const lerp = (a, b, t) => a + (b - a) * Math.max(0, Math.min(1, t));
    const lerpColor = (c1, c2, t) => {
      const r1 = parseInt(c1.slice(1, 3), 16),
        g1 = parseInt(c1.slice(3, 5), 16),
        b1 = parseInt(c1.slice(5, 7), 16);
      const r2 = parseInt(c2.slice(1, 3), 16),
        g2 = parseInt(c2.slice(3, 5), 16),
        b2 = parseInt(c2.slice(5, 7), 16);
      const r = Math.round(lerp(r1, r2, t)),
        g = Math.round(lerp(g1, g2, t)),
        b = Math.round(lerp(b1, b2, t));
      return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
    };
    const t = (altitudeDeg + 12) / 27;
    let exaggeration;
    if (altitudeDeg < -6) {
      exaggeration = lerp(0.2, 0.5, (altitudeDeg + 12) / 6);
    } else if (altitudeDeg < 5) {
      exaggeration = lerp(0.5, 1.0, (altitudeDeg + 6) / 11);
    } else {
      exaggeration = 1.0;
    }
    const shadowColor = lerpColor("#222222", "#ffffff", t);
    const highlightColor = lerpColor("#111111", "#000000", t);
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
        el.style.cssText = `width:24px;height:24px;background:${invertColor(dayColor)};border:3px solid black;cursor:pointer;`;
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
              "hillshade-accent-color": "#000000",
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
          // Thick black line: inverts to a clean white route.
          map.addLayer({
            id: "route-line",
            type: "line",
            source: "route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: { "line-color": "#000000", "line-width": 7, "line-opacity": 1 },
          });
          // Thin inverted-day-colour accent down the centre.
          map.addLayer({
            id: "route-color-accent",
            type: "line",
            source: "route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: {
              "line-color": invertColor(dayColor),
              "line-width": 1.5,
              "line-opacity": 1,
            },
          });
          // Wide transparent hit area for easier clicking.
          map.addLayer({
            id: "route-hit-area",
            type: "line",
            source: "route",
            layout: { "line-join": "round", "line-cap": "round" },
            paint: { "line-color": "transparent", "line-width": 20, "line-opacity": 0 },
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

        // Start / end DOM markers (inverted: black draws white, white draws
        // black). The square current-photo marker is created by the $effect.
        const pts = geoPoints();
        if (pts.length > 0) {
          const startEl = document.createElement("div");
          startEl.style.cssText =
            "width:16px;height:16px;background:#000000;border:3px solid #ffffff;border-radius:50%;"; /* nosemgrep: svelte-hardcoded-color-in-style */
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
              "width:14px;height:14px;background:#ffffff;border:3px solid #000000;border-radius:50%;"; /* nosemgrep: svelte-hardcoded-color-in-style */
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
  /* The whole map is inverted so the light Carto basemap reads dark; every
     colour drawn into it is pre-inverted to compensate. */
  .map {
    width: 100%;
    overflow: hidden;
    filter: invert(1);
  }
</style>
