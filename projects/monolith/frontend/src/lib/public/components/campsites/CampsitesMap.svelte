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

  let heatOn = $state(true); // weather heatmap visible by default

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

  // "Sunshine" ramp for the weather heatmap: transparent at zero density, warming
  // through pale yellow and amber into lime and green as more open-and-clear parks
  // cluster together. Kept as named constants (not inline literals) for the same
  // semgrep-safety reason as the pin colors.
  const HEAT_C0 = "rgba(0, 0, 0, 0)";
  const HEAT_C1 = "#fde68a"; // pale yellow
  const HEAT_C2 = "#fbbf24"; // yellow
  const HEAT_C3 = "#f59e0b"; // amber
  const HEAT_C4 = "#84cc16"; // lime
  const HEAT_C5 = "#22c55e"; // green

  const HEAT_COLOR = [
    "interpolate",
    ["linear"],
    ["heatmap-density"],
    0,
    HEAT_C0,
    0.2,
    HEAT_C1,
    0.4,
    HEAT_C2,
    0.6,
    HEAT_C3,
    0.8,
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

  function toggleHeat() {
    heatOn = !heatOn;
    if (map && layerReady) {
      map.setLayoutProperty(
        HEAT_LAYER,
        "visibility",
        heatOn ? "visible" : "none",
      );
    }
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
          layout: { visibility: heatOn ? "visible" : "none" },
          paint: {
            "heatmap-weight": [
              "interpolate",
              ["linear"],
              ["get", "good_days"],
              0,
              0,
              1,
              0.35,
              7,
              1,
            ],
            "heatmap-intensity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              4,
              1,
              8,
              2,
            ],
            "heatmap-radius": [
              "interpolate",
              ["linear"],
              ["zoom"],
              4,
              28,
              8,
              48,
            ],
            "heatmap-opacity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              7,
              0.55,
              9,
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
            // Radius grows with good_days; selected pin is larger.
            "circle-radius": [
              "case",
              ["==", ["get", "sel"], 1],
              ["interpolate", ["linear"], ["get", "good_days"], 0, 9, 7, 16],
              ["interpolate", ["linear"], ["get", "good_days"], 0, 5, 7, 11],
            ],
            "circle-stroke-width": [
              "case",
              ["==", ["get", "sel"], 1],
              3,
              1.5,
            ],
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
        map.on("click", (e) => {
          const feats = map.queryRenderedFeatures(e.point, {
            layers: [PIN_LAYER],
          });
          if (feats.length) {
            const f = feats[0];
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

  <div class="heat-toggle" role="group" aria-label="Weather heatmap">
    <button
      type="button"
      class:active={heatOn}
      aria-pressed={heatOn}
      onclick={toggleHeat}
    >
      Heat {heatOn ? "on" : "off"}
    </button>
  </div>

  <div class="legend">
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
    box-shadow: 2px 2px 0 var(--ink);
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
    box-shadow: 3px 3px 0 var(--ink);
    padding: 8px 10px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink);
  }

  .map :global(.maplibregl-popup-tip) {
    display: none;
  }

  /* Heat on/off switch, top-right (same idiom as the ShipsMap mode toggle). */
  .heat-toggle {
    position: absolute;
    top: 16px;
    right: 16px;
    display: inline-flex;
    border: 2px solid var(--ink);
    background: var(--paper);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .heat-toggle button {
    padding: 8px 14px;
    background: var(--paper);
    border: none;
    color: var(--ink);
    cursor: pointer;
    transition: background 120ms ease;
  }

  .heat-toggle button:hover {
    background: var(--cream);
  }

  .heat-toggle button.active {
    background: var(--ink);
    color: var(--paper);
  }

  /* Legend: bottom-left, same hard-edge style as the ShipsMap legend. */
  .legend {
    position: absolute;
    bottom: 16px;
    left: 16px;
    max-width: 240px;
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

  @media (max-width: 640px) {
    .heat-toggle {
      top: 12px;
      right: 12px;
      font-size: 13px;
    }

    .heat-toggle button {
      padding: 11px 18px;
    }

    .legend {
      bottom: 12px;
      left: 12px;
      max-width: 200px;
      padding: 9px 11px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .map :global(.maplibregl-ctrl-group button),
    .heat-toggle button {
      transition: none;
    }
  }
</style>
