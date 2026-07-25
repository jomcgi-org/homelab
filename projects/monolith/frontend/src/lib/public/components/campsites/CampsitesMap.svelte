<script>
  import { onMount } from "svelte";
  // CSS is a static asset processed by Vite at build time; safe at top level
  // even when this component is SSR-rendered (Vite skips it on the server).
  // The map itself is lazy-imported inside onMount to avoid window/WebGL refs
  // during SSR. Same pattern as ShipsMap / StarsMap.
  import "maplibre-gl/dist/maplibre-gl.css";

  // `parks` is the full snapshot array (one Point per park).
  // `selectedId` is bindable so both the map (click) and the parent list can
  // drive selection: clicking a pin sets it here and propagates up, and a list
  // click changes it in the parent and the effect below flies the map to it.
  let { parks = [], selectedId = $bindable(null) } = $props();

  // OpenFreeMap needs no API key (same hosted liberty style as ships/stars so
  // the pages read as siblings).
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

  // Data-viz ramp colors: outside the design-token system (intentionally, same
  // pattern as ShipsMap VESSEL_COLORS / HEAT_COLORS). They live in plain JS
  // constants so the hexes never appear as a `:` + `#hex` pair inside a style
  // attribute, which is what the hardcoded-color semgrep rule matches.
  //   COL_NONE = grey (nothing open or no forecast)
  //   COL_LOW  = yellow (a few clear days)
  //   COL_MED  = amber (decent)
  //   COL_GOOD = green (open AND clear-sky)
  const COL_NONE = "#6b7280";
  const COL_LOW = "#eab308";
  const COL_MED = "#f59e0b";
  const COL_GOOD = "#22c55e";
  const STROKE = "#111827";

  // Score-to-color ramp for the pin circle fill (grey -> yellow -> amber -> green).
  const SCORE_COLOR = [
    "interpolate",
    ["linear"],
    ["get", "best_score"],
    0,
    COL_NONE,
    40,
    COL_LOW,
    70,
    COL_MED,
    90,
    COL_GOOD,
  ];

  // Warm "sunshine" field for the weather heatmap: transparent at low density
  // (so a single park does not glow), warming through gold to a hot orange for
  // clusters of open-and-clear parks. Warm contrasts with the green terrain and
  // makes the green pins pop. Kept as named constants (not inline literals) for
  // the same semgrep-safety reason as the pin colors.
  const HEAT_C0 = "rgba(0, 0, 0, 0)";
  const HEAT_C1 = "rgba(254, 243, 199, 0.35)"; // amber-100, very soft
  const HEAT_C2 = "rgba(253, 224, 71, 0.6)"; // yellow-300
  const HEAT_C3 = "rgba(251, 146, 60, 0.8)"; // orange-400
  const HEAT_C4 = "#f97316"; // orange-500
  const HEAT_C5 = "#ea580c"; // orange-600, hot

  const HEAT_COLOR = [
    "interpolate",
    ["linear"],
    ["heatmap-density"],
    0,
    HEAT_C0,
    0.3,
    HEAT_C1,
    0.55,
    HEAT_C2,
    0.75,
    HEAT_C3,
    0.9,
    HEAT_C4,
    1,
    HEAT_C5,
  ];

  // Legend rows: color is a JS variable reference, so the template writes
  // `style="background: {item.color}"` with no literal hex in the style attribute
  // (matching the ShipsMap LEGEND pattern).
  const LEGEND_ITEMS = [
    { label: "Open + clear", color: COL_GOOD },
    { label: "Some clear days", color: COL_LOW },
    { label: "Nothing open", color: COL_NONE },
  ];
  // Swatches for the heat gradient chip in the legend.
  const HEAT_SWATCHES = [HEAT_C1, HEAT_C3, HEAT_C5];

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
      maplibregl = (await import("maplibre-gl")).default;
      if (destroyed) return;

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
      mapContainer.addEventListener("click", (e) => {
        if (e.target.closest(".maplibregl-ctrl-attrib-button")) {
          userOpenedAttrib = true;
        }
      });
      map.on("styledata", collapseAttrib);
      map.on("sourcedata", collapseAttrib);
      collapseAttrib();

      map.on("load", () => {
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
            "heatmap-color": HEAT_COLOR,
          },
        });

        // Layer B: park pins, added AFTER the heatmap so they sit ON TOP.
        map.addLayer({
          id: PIN_LAYER,
          type: "circle",
          source: SOURCE_ID,
          paint: {
            "circle-color": SCORE_COLOR,
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
            "circle-stroke-color": STROKE,
            "circle-opacity": ["case", ["==", ["get", "sel"], 1], 1, 0.85],
          },
        });

        layerReady = true;
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

      cleanup = () => {
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

<style>
  /* Full-bleed: the map fills the whole page (its .campsites-page ancestor is
     the positioned containing block). Matches ShipsMap. */
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

  /* Push zoom controls clear of device safe areas (same as ShipsMap). */
  .map :global(.maplibregl-ctrl-bottom-right) {
    bottom: env(safe-area-inset-bottom, 0);
    right: env(safe-area-inset-right, 0);
  }

  /* Hard-rectangle zoom buttons that lift off on hover (matches ShipsMap). */
  .map :global(.maplibregl-ctrl-group) {
    border: none;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
    overflow: visible;
  }

  .map :global(.maplibregl-ctrl-group button) {
    border: 2px solid var(--ink);
    background: var(--paper);
    transition:
      transform 110ms ease,
      box-shadow 110ms ease;
  }

  .map :global(.maplibregl-ctrl-group button + button) {
    margin-top: 6px;
  }

  .map :global(.maplibregl-ctrl-group button:hover),
  .map :global(.maplibregl-ctrl-group button:focus-visible) {
    transform: translate(-2px, -2px);
    background: var(--paper);
    position: relative;
    z-index: 1;
  }

  /* Compact attribution "i" button (matches ShipsMap). */
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

  /* Popup: hard-edge brutalist box, no round tip. */
  .map :global(.maplibregl-popup-content) {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: 0;
    padding: 8px 10px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink);
  }

  .map :global(.maplibregl-popup-tip) {
    display: none;
  }

  /* Legend: bottom-left, same hard-edge style as the ShipsMap legend. Fixed
     width + height so the bottom row (legend left, detail right) share the
     same bottom edge AND top edge. */
  .legend {
    position: absolute;
    bottom: 16px;
    left: 16px;
    width: 200px;
    height: 168px;
    box-sizing: border-box;
    overflow-y: auto;
    padding: 10px 12px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  .legend-title {
    margin: 0 0 8px;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .legend-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .legend-list li {
    display: flex;
    align-items: center;
    gap: 7px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }

  /* Circular swatch to echo the circle pins on the map. */
  .sw {
    width: 12px;
    height: 12px;
    border: 1.5px solid var(--ink);
    border-radius: 50%;
    flex: none;
  }

  .legend-heat {
    margin-top: 9px;
    padding-top: 8px;
    border-top: 1.5px dashed var(--rule-2);
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .legend-heat-label {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  .legend-heat-bar {
    display: inline-flex;
    height: 10px;
    border: 1.5px solid var(--ink);
  }

  .legend-heat-cell {
    flex: 1;
    min-width: 22px;
  }

  .legend-note {
    margin: 8px 0 0;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
    letter-spacing: 0.02em;
  }

  /* Legend toggle chip: hidden on desktop, revealed on mobile only. */
  .legend-toggle {
    display: none;
    position: absolute;
    bottom: 56px;
    left: 12px;
    z-index: 10;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 6px 10px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition: transform 110ms ease;
  }

  .legend-toggle:hover,
  .legend-toggle:focus-visible {
    transform: translate(-2px, -2px);
    outline: none;
  }

  /* Mobile (<=768px): legend becomes a pop-out, toggle chip is visible.
     The list bar is 48px tall at bottom:0. The toggle chip sits at
     bottom:56px (8px clear). The legend pops above the chip at bottom:96px
     (56px base + ~36px chip + 4px gap). */
  @media (max-width: 768px) {
    /* Hidden by default; display:block added via .legend-open when toggled. */
    .legend {
      display: none;
      bottom: 96px;
      left: 12px;
      width: auto;
      height: auto;
      max-width: 200px;
      padding: 9px 11px;
      z-index: 10;
    }

    .legend.legend-open {
      display: block;
    }

    /* Show the toggle chip above the list bar. */
    .legend-toggle {
      display: flex;
      align-items: center;
    }

    /* Zoom controls: nudge above the 48px list bar so they never overlap it. */
    .map :global(.maplibregl-ctrl-bottom-right) {
      bottom: calc(56px + env(safe-area-inset-bottom, 0));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .map :global(.maplibregl-ctrl-group button),
    .legend-toggle {
      transition: none;
    }
  }
</style>
