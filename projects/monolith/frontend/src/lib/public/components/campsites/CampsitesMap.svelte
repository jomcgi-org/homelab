<script>
  import { onMount } from "svelte";
  // CSS is a static asset processed by Vite at build time; safe at top level
  // even when this component is SSR-rendered (Vite skips it on the server).
  // The map itself is lazy-imported inside onMount to avoid window/WebGL refs
  // during SSR. Same pattern as ShipsMap / StarsMap.
  import "maplibre-gl/dist/maplibre-gl.css";
  // MapLibre 6 computes a sibling worker URL dynamically. Give Vite an
  // explicit worker asset so both dev and production bundles ship it.
  import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
  import {
    applyPalette,
    heatPaint,
    readPalette,
    scorePaint,
  } from "./map-theme.js";

  // `parks` is the parent's filtered, re-scored snapshot (one Point per park).
  // `selectedId` is bindable so both the map (click) and the parent list can
  // drive selection: clicking a pin sets it here and propagates up, and a list
  // click changes it in the parent and the effect below flies the map to it.
  let { parks = [], selectedId = $bindable(null) } = $props();

  // OpenFreeMap needs no API key. Its sources and geometry stay intact while
  // configurable paint follows the sheet's light/dark tokens.
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
  const SOURCE_ID = "parks";
  const PIN_LAYER = "parks";
  const HEAT_LAYER = "weather-heat";

  let maplibregl; // loaded lazily in onMount (browser-only)
  let mapContainer; // bound <div>
  let map = null;
  let popup = null;
  let resizeObs = null;
  let layerReady = $state(false); // $state so effects re-evaluate after load

  // Legend pop-out state: only relevant on mobile; on desktop the legend is
  // always visible and the toggle button is hidden via CSS.
  let legendOpen = $state(false);
  let legendEl; // DOM ref for click-outside detection
  let toggleEl; // DOM ref for click-outside detection

  // Close the legend when the user taps anywhere outside it on mobile.
  $effect(() => {
    if (!legendOpen) return;
    function onDocPointerdown(e) {
      if (
        legendEl &&
        !legendEl.contains(e.target) &&
        toggleEl &&
        !toggleEl.contains(e.target)
      ) {
        legendOpen = false;
      }
    }
    document.addEventListener("pointerdown", onDocPointerdown, {
      capture: true,
    });
    return () =>
      document.removeEventListener("pointerdown", onDocPointerdown, {
        capture: true,
      });
  });

  // Legend swatches share the map's semantic sheet roles.
  const LEGEND_ITEMS = [
    { label: "Open + clear", color: "var(--camp-clear)" },
    { label: "Some clear days", color: "var(--camp-mixed)" },
    { label: "Nothing open", color: "var(--camp-none)" },
  ];
  const HEAT_SWATCHES = [
    "var(--camp-mixed)",
    "var(--camp-poor)",
    "var(--tone-disk)",
  ];

  // Build the GeoJSON FeatureCollection from the current parks + selectedId. A
  // `sel` property (0/1) drives the pin radius and stroke expressions so the
  // selected marker stands out without a separate layer.
  function buildFC() {
    return {
      type: "FeatureCollection",
      features: parks.map((p) => ({
        type: "Feature",
        id: p.id,
        geometry: { type: "Point", coordinates: [p.lon, p.lat] },
        properties: {
          id: p.id,
          name: p.name,
          region: p.region,
          best_score: p.best_score,
          good_days: p.good_days,
          sel: p.id === selectedId ? 1 : 0,
        },
      })),
    };
  }

  // Fit the viewport to the full park set on first load, with some padding.
  function fitParks() {
    if (!map || !parks.length) return;
    const lons = parks.map((p) => p.lon);
    const lats = parks.map((p) => p.lat);
    map.fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: 56, maxZoom: 9, duration: 0 },
    );
  }

  function daysText(n) {
    return n === 1 ? "1 clear day" : `${n} clear days`;
  }

  // Re-sync the source whenever parks or selection changes. Runs once map and
  // layer are ready (layerReady is $state, so the effect re-evaluates then).
  $effect(() => {
    void parks;
    void selectedId;
    if (!map || !layerReady) return;
    map.getSource(SOURCE_ID)?.setData(buildFC());
  });

  // Fly to the newly selected park when selection changes (list row or map
  // click both funnel through selectedId). The layerReady guard also means this
  // runs once after initial load if selectedId is already set.
  $effect(() => {
    const id = selectedId;
    if (!id || !map || !layerReady) return;
    const park = parks.find((p) => p.id === id);
    if (park) {
      map.flyTo({
        center: [park.lon, park.lat],
        zoom: Math.max(map.getZoom(), 7),
        duration: 500,
      });
    }
  });

  onMount(() => {
    let destroyed = false;
    let cleanup = () => {};

    (async () => {
      maplibregl = await import("maplibre-gl");
      if (destroyed) return;
      maplibregl.setWorkerUrl(workerUrl);

      map = new maplibregl.Map({
        container: mapContainer,
        style: BASEMAP_STYLE,
        // Default view centered on BC; fitParks() overrides this on load.
        center: [-125, 54.5],
        zoom: 4.4,
        attributionControl: { compact: true },
      });

      map.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );

      // Compact popup for click; positioned at the pin, no default tip arrow.
      popup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 12,
      });

      // Collapse the attribution on every styledata/sourcedata event so it never
      // renders as a full-width credit bar. Same pattern as ShipsMap.
      let userOpenedAttrib = false;
      const collapseAttrib = () => {
        if (userOpenedAttrib) return;
        const el = mapContainer.querySelector(".maplibregl-ctrl-attrib");
        el?.removeAttribute("open");
        el?.classList.remove("maplibregl-compact-show");
      };
      const openAttribution = (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      };
      mapContainer.addEventListener("click", openAttribution);
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

      map.on("load", () => {
        const palette = readPalette(mapContainer);
        map.addSource(SOURCE_ID, { type: "geojson", data: buildFC() });

        // Layer A: weather heatmap, added FIRST so it sits UNDER the pins. Weight
        // rides on good_days (open AND clear parks), so regions with more good
        // camping glow. Fades out at high zoom so the pins dominate up close.
        map.addLayer({
          id: HEAT_LAYER,
          type: "heatmap",
          source: SOURCE_ID,
          layout: { visibility: "visible" },
          paint: {
            "heatmap-weight": [
              "interpolate",
              ["linear"],
              ["get", "good_days"],
              0,
              0,
              2,
              0.12,
              4,
              0.45,
              7,
              1,
            ],
            "heatmap-intensity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              4,
              0.85,
              8,
              1.5,
            ],
            "heatmap-radius": [
              "interpolate",
              ["linear"],
              ["zoom"],
              4,
              45,
              6,
              62,
              9,
              34,
            ],
            "heatmap-opacity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              4,
              0.5,
              8,
              0.45,
              10,
              0,
            ],
            "heatmap-color": heatPaint(palette),
          },
        });

        // Layer B: park pins, added AFTER the heatmap so they sit ON TOP.
        map.addLayer({
          id: PIN_LAYER,
          type: "circle",
          source: SOURCE_ID,
          paint: {
            "circle-color": scorePaint(palette),
            // Radius grows with good_days (more clear days = bigger pin), with
            // the selected pin larger still, and the outer ["zoom"] interpolate
            // makes pins also grow as you zoom in. Zoom-and-property form: the
            // ["zoom"] interpolate is outermost and its stop outputs are
            // per-feature expressions (valid because the inner expressions never
            // read zoom).
            //
            // MINIMUM SIZE FLOOR: the smallest a pin ever renders is the zoomed
            // -out, zero-clear-days, unselected case = radius 12 (24px diameter).
            // interpolate clamps to the first stop below zoom 5, so this floor
            // also holds at the initial full-BC view (~z4.4). 24px visual + the
            // 14px queryRenderedFeatures tap box on each side gives a ~52px
            // effective touch target, comfortably finger-pressable on a phone
            // without the ~145 province-wide pins merging into blobs.
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["zoom"],
              // Zoomed out (province view): finger-friendly floor.
              5,
              [
                "case",
                ["==", ["get", "sel"], 1],
                ["interpolate", ["linear"], ["get", "good_days"], 0, 15, 7, 20],
                ["interpolate", ["linear"], ["get", "good_days"], 0, 12, 7, 16],
              ],
              // Zoomed in: pins swell so a selected park is unmistakable.
              11,
              [
                "case",
                ["==", ["get", "sel"], 1],
                ["interpolate", ["linear"], ["get", "good_days"], 0, 22, 7, 30],
                ["interpolate", ["linear"], ["get", "good_days"], 0, 18, 7, 24],
              ],
              // Deep zoom (single park fills the screen, e.g. tracing a trail):
              // keep growing so the pin never reads as a tiny dot against a
              // massive path.
              16,
              [
                "case",
                ["==", ["get", "sel"], 1],
                ["interpolate", ["linear"], ["get", "good_days"], 0, 34, 7, 44],
                ["interpolate", ["linear"], ["get", "good_days"], 0, 28, 7, 38],
              ],
            ],
            "circle-stroke-width": ["case", ["==", ["get", "sel"], 1], 3, 1.5],
            "circle-stroke-color": palette.ink,
            "circle-opacity": ["case", ["==", ["get", "sel"], 1], 1, 0.85],
          },
        });

        layerReady = true;
        applyPalette(map, palette);
        fitParks();
        // Belt-and-suspenders against a container that was still zero-sized when
        // the map initialised (the split-layout race that rendered blank before).
        map.resize();

        // Click a pin to select it (drives the bindable + the parent detail
        // panel); click the background to deselect.
        //
        // Hit-test a padded BOX around the tap, not the exact pixel: pins are
        // only a handful of pixels wide, and on a touch screen a fingertip
        // never lands dead-center, so an exact-point query almost always missed
        // and silently deselected (Anna's "buttons don't bring up info"). The
        // box gives a forgiving ~TAP_PAD-px tolerance in every direction; when
        // several pins fall inside it we pick the one nearest the tap.
        const TAP_PAD = 14; // px of slop around the tap point
        map.on("click", (e) => {
          const box = [
            [e.point.x - TAP_PAD, e.point.y - TAP_PAD],
            [e.point.x + TAP_PAD, e.point.y + TAP_PAD],
          ];
          const feats = map.queryRenderedFeatures(box, {
            layers: [PIN_LAYER],
          });
          if (feats.length) {
            // Nearest pin to the actual tap wins, so a box that catches two
            // neighbours still selects the one the finger was closest to.
            const f = feats.reduce((best, cand) => {
              const bp = map.project(best.geometry.coordinates);
              const cp = map.project(cand.geometry.coordinates);
              const bd = (bp.x - e.point.x) ** 2 + (bp.y - e.point.y) ** 2;
              const cd = (cp.x - e.point.x) ** 2 + (cp.y - e.point.y) ** 2;
              return cd < bd ? cand : best;
            });
            selectedId = f.properties.id;
            const { name, best_score, good_days } = f.properties;
            popup
              .setLngLat(f.geometry.coordinates.slice())
              .setHTML(
                `<strong>${name}</strong><br>Score ${best_score} &middot; ${daysText(good_days)}`,
              )
              .addTo(map);
          } else {
            selectedId = null;
            popup.remove();
          }
        });

        map.on("mouseenter", PIN_LAYER, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", PIN_LAYER, () => {
          map.getCanvas().style.cursor = "";
        });
      });

      // A late-sized container (grid/flex settling after mount) can leave the
      // GL canvas at 0x0 and paint blank. Observe the container and resize the
      // map whenever its box changes, so it always fills the viewport.
      resizeObs = new ResizeObserver(() => map?.resize());
      resizeObs.observe(mapContainer);

      // SchemeToggle stamps data-theme. Also follow OS changes when there is
      // no explicit choice. Never replace the style or re-register map events.
      const repaint = () => {
        if (map && layerReady) applyPalette(map, readPalette(mapContainer));
      };
      const schemeObserver = new MutationObserver(repaint);
      schemeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
      const media = matchMedia("(prefers-color-scheme: dark)");
      media.addEventListener("change", repaint);

      cleanup = () => {
        schemeObserver.disconnect();
        media.removeEventListener("change", repaint);
        mapContainer.removeEventListener("click", openAttribution);
        map?.off("styledata", collapseAttrib);
        map?.off("sourcedata", collapseAttrib);
        resizeObs?.disconnect();
        resizeObs = null;
        popup?.remove();
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

  <!-- Legend: always visible on desktop; a pop-out toggled by the chip on mobile. -->
  <div
    class="legend"
    id="campsites-legend"
    class:legend-open={legendOpen}
    bind:this={legendEl}
  >
    <p class="legend-title">Open sites + clear sky</p>
    <ul class="legend-list">
      {#each LEGEND_ITEMS as item (item.label)}
        <li>
          <span class="sw" style="background: {item.color}" aria-hidden="true"
          ></span>{item.label}
        </li>
      {/each}
    </ul>
    <div class="legend-heat">
      <span class="legend-heat-label">Heatmap = density of good camping</span>
      <span class="legend-heat-bar" aria-hidden="true">
        {#each HEAT_SWATCHES as c (c)}
          <span class="legend-heat-cell" style="background: {c}"></span>
        {/each}
      </span>
    </div>
    <p class="legend-note">Pin size = clear-sky days</p>
  </div>

  <!-- Mobile-only chip to open/close the legend pop-out.
       Hidden on desktop via CSS; always a real focusable button. -->
  <button
    type="button"
    class="legend-toggle"
    aria-expanded={legendOpen}
    aria-controls="campsites-legend"
    onclick={() => (legendOpen = !legendOpen)}
    bind:this={toggleEl}>{legendOpen ? "Close" : "Legend"}</button
  >
</div>
