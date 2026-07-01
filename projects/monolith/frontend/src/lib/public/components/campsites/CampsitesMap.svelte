<script>
  import { onMount } from "svelte";
  // CSS is a static asset processed by Vite at build time; safe at top level
  // even when this component is SSR-rendered (Vite skips it on the server).
  // The map itself is lazy-imported inside onMount to avoid window/WebGL refs
  // during SSR. Same pattern as ShipsMap / HikesMap.
  import "maplibre-gl/dist/maplibre-gl.css";

  // `parks` is the full snapshot array (Points for each park).
  // `selectedId` is bindable so both the map (click) and the parent list can
  // drive selection; clicking a circle sets it here and propagates up, and a
  // list click changes it in the parent and the effect below pans the map.
  let { parks = [], selectedId = $bindable(null) } = $props();

  // OpenFreeMap needs no API key (same hosted liberty style as ships/hikes so
  // the pages read as siblings).
  const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
  const SOURCE_ID = "parks";
  const LAYER_ID = "parks-layer";

  let maplibregl; // loaded lazily in onMount (browser-only)
  let mapContainer; // bound <div>
  let map = null;
  let popup = null;
  let layerReady = $state(false); // $state so effects re-evaluate after load

  // Data-viz ramp colors: outside the design-token system (intentionally, same
  // pattern as ShipsMap VESSEL_COLORS / HEAT_COLORS). nosemgrep suppressed on
  // template usages where the hex appears inside a JS expression, not a literal
  // CSS property value.
  //   COL_NONE  = grey (nothing open or no forecast)
  //   COL_LOW   = yellow (a few ok days)
  //   COL_MED   = amber (decent)
  //   COL_GOOD  = green (open AND clear-sky)
  const COL_NONE = "#6b7280";
  const COL_LOW  = "#eab308";
  const COL_MED  = "#f59e0b";
  const COL_GOOD = "#22c55e";

  // Score-to-color ramp for the MapLibre circle fill expression.
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

  // Legend items: color is a JS variable reference, so the template can write
  // `style="background: {item.color}"` without embedding a literal hex in the
  // style attribute (matching the ShipsMap LEGEND pattern).
  const LEGEND_ITEMS = [
    { label: "Open + clear", color: COL_GOOD },
    { label: "Some clear days", color: COL_LOW },
    { label: "Nothing open", color: COL_NONE },
  ];

  // Build the GeoJSON FeatureCollection from the current parks + selectedId.
  // A `sel` property (0/1) drives the circle-radius and stroke expressions so
  // the selected marker stands out without a separate layer.
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
      { padding: 40, maxZoom: 9, duration: 0 },
    );
  }

  // Re-sync the source whenever parks or selection changes. Runs once map and
  // layer are ready (layerReady is $state, so the effect re-evaluates then).
  $effect(() => {
    // Explicit reads so Svelte tracks both as dependencies.
    void parks;
    void selectedId;
    if (!map || !layerReady) return;
    map.getSource(SOURCE_ID)?.setData(buildFC());
  });

  // Pan to the newly selected park when selection changes (list or URL drive).
  // The `layerReady` guard also means this runs once after initial load if
  // selectedId is already set (e.g. restored from URL state in the parent).
  $effect(() => {
    const id = selectedId;
    if (!id || !map || !layerReady) return;
    const park = parks.find((p) => p.id === id);
    if (park) {
      map.easeTo({
        center: [park.lon, park.lat],
        zoom: Math.max(map.getZoom(), 7),
        duration: 400,
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
        center: [-125, 54],
        zoom: 4.6,
        attributionControl: { compact: true },
      });

      map.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );

      // Compact popup for hover; positioned at cursor, no default tip arrow.
      popup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 10,
      });

      // Collapse the attribution on every styledata/sourcedata event so it
      // never renders as a full-width credit bar. Same pattern as ShipsMap.
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

        map.addLayer({
          id: LAYER_ID,
          type: "circle",
          source: SOURCE_ID,
          paint: {
            // Fill: interpolated by best_score (grey -> yellow -> green).
            "circle-color": SCORE_COLOR,
            // Radius grows with good_days; selected marker is larger.
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
            "circle-stroke-color": "#111827",
            "circle-opacity": [
              "case",
              ["==", ["get", "sel"], 1],
              1,
              0.85,
            ],
          },
        });

        layerReady = true;
        fitParks();

        // Click handler: set selection or deselect on background click.
        map.on("click", (e) => {
          const feats = map.queryRenderedFeatures(e.point, {
            layers: [LAYER_ID],
          });
          if (feats.length) {
            selectedId = feats[0].properties.id;
          } else {
            selectedId = null;
          }
        });

        // Hover popup: park name + good_days count.
        map.on("mouseenter", LAYER_ID, (e) => {
          map.getCanvas().style.cursor = "pointer";
          const f = e.features?.[0];
          if (!f) return;
          const { name, good_days } = f.properties;
          const daysText =
            good_days === 1 ? "1 clear day" : `${good_days} clear days`;
          popup
            .setLngLat(e.lngLat)
            .setHTML(`<strong>${name}</strong><br>${daysText}`)
            .addTo(map);
        });

        map.on("mouseleave", LAYER_ID, () => {
          map.getCanvas().style.cursor = "";
          popup.remove();
        });
      });

      cleanup = () => {
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

  <div class="legend">
    <p class="legend-title">Open sites + clear sky</p>
    <ul class="legend-list">
      {#each LEGEND_ITEMS as item (item.label)}
        <li>
          <span class="sw" style="background: {item.color}" aria-hidden="true"></span>{item.label}
        </li>
      {/each}
    </ul>
    <p class="legend-note">Circle size = clear-sky days</p>
  </div>
</div>

<style>
  .map-wrap {
    position: relative;
    width: 100%;
    height: 100%;
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
    transition: transform 110ms ease, box-shadow 110ms ease;
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

  /* Legend: bottom-left, same hard-edge style as ShipsMap legend. */
  .legend {
    position: absolute;
    bottom: 16px;
    left: 16px;
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

  /* Circular swatch to echo the circle markers on the map. */
  .sw {
    width: 12px;
    height: 12px;
    border: 1.5px solid var(--ink);
    border-radius: 50%;
    flex: none;
  }

  .legend-note {
    margin: 7px 0 0;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
    letter-spacing: 0.02em;
  }
</style>
