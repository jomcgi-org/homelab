// This module, the MapLibre CSS and its worker are loaded only after map intent.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Map as LibreMap,
  LngLatBounds,
  NavigationControl,
  setWorkerUrl,
  type GeoJSONSource,
  type ExpressionSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { cells, type MapData } from "./stars-geometry";
import type { Site } from "./stars-model";
import { isMapFailure } from "./map-errors";

export default function StarsMap(
  props: MapData & { onSelect: (id: Site["id"] | null) => void },
) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<LibreMap | null>(null);
  const fitted = useRef(false);
  const onSelect = useRef(props.onSelect);
  onSelect.current = props.onSelect;
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const data = useMemo(
    () => cells(props),
    [props.sites, props.mode, props.darkness, props.activeNights, props.now],
  );
  useEffect(() => {
    const container = host.current!;
    let instance: LibreMap;
    setReady(false);
    setFailed(false);
    fitted.current = false;
    try {
      setWorkerUrl(workerUrl);
      instance = new LibreMap({
        container,
        center: [-4.2, 57],
        zoom: 5.5,
        attributionControl: { compact: true },
      });
      instance.setStyle("https://tiles.openfreemap.org/styles/liberty");
    } catch {
      setFailed(true);
      return;
    }
    map.current = instance;
    instance.addControl(
      new NavigationControl({ showCompass: false }),
      "bottom-right",
    );
    const ramp = (): ExpressionSpecification => {
      const swatch = (level: string) =>
        getComputedStyle(
          container.parentElement!.querySelector(`.stars-${level}`)!,
        ).backgroundColor;
      return [
        "interpolate",
        ["linear"],
        ["get", "score"],
        0,
        swatch("low"),
        50,
        swatch("mid"),
        100,
        swatch("high"),
      ];
    };
    const canvas = instance.getCanvas();
    canvas.setAttribute("aria-label", "Dark-sky sites");
    canvas.setAttribute(
      "aria-description",
      "Arrow keys pan, plus and minus zoom, Enter selects a site at the map centre. The site list also opens site details.",
    );
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") onSelect.current(null);
      if (event.key !== "Enter" || !instance.getLayer("stars-cells")) return;
      event.preventDefault();
      const feature = instance.queryRenderedFeatures(
        instance.project(instance.getCenter()),
        { layers: ["stars-cells"] },
      )[0];
      if (feature) onSelect.current(feature.properties.id as Site["id"]);
    };
    canvas.addEventListener("keydown", keyboard);
    instance.on("load", () => {
      instance.addSource("stars", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      instance.addLayer({
        id: "stars-cells",
        type: "fill",
        source: "stars",
        paint: {
          "fill-color": ramp(),
          "fill-opacity": 0.72,
          "fill-antialias": false,
        },
      });
      setReady(true);
      setFailed(false);
    });
    instance.on("click", "stars-cells", (event) => {
      const feature = event.features?.[0];
      if (feature) onSelect.current(feature.properties.id as Site["id"]);
    });
    instance.on("error", (event) => {
      if (isMapFailure(event)) setFailed(true);
    });
    const recolor = () => {
      if (instance.getLayer("stars-cells"))
        instance.setPaintProperty("stars-cells", "fill-color", ramp());
    };
    const scheme = matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", recolor);
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(container);
    return () => {
      observer.disconnect();
      scheme.removeEventListener("change", recolor);
      canvas.removeEventListener("keydown", keyboard);
      instance.remove();
      map.current = null;
    };
  }, [attempt]);
  useEffect(() => {
    if (!ready) return;
    // Passing objects also works for history; no data: fetch or CSP exception.
    (map.current?.getSource("stars") as GeoJSONSource | undefined)?.setData(
      data,
    );
    if (!fitted.current && props.sites.length && map.current) {
      const bounds = new LngLatBounds();
      for (const site of props.sites) {
        if (
          Number.isFinite(site.lat) &&
          Number.isFinite(site.lon) &&
          Math.abs(site.lat) <= 90 &&
          Math.abs(site.lon) <= 180
        )
          bounds.extend([site.lon, site.lat]);
      }
      if (!bounds.isEmpty()) {
        map.current.fitBounds(bounds, { padding: 48, maxZoom: 8, duration: 0 });
        fitted.current = true;
      }
    }
  }, [data, ready, props.sites]);
  return (
    <>
      {failed && (
        <p role="status">
          The map could not load. The site list and details remain available.{" "}
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>
            Retry map
          </button>
        </p>
      )}
      <div className="stars-map" ref={host} />
    </>
  );
}
