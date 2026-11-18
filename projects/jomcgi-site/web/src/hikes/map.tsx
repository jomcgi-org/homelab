// This whole module (including the style and worker) is requested only by the
// Show map button. Passing GeoJSON objects avoids a data: connect-src exception.
import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import type { GeoJSONSource } from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Walk } from "./data";
import { effort, effortCeilings } from "./filters";

export interface HikesMapProps {
  walks: Walk[];
  corpus: Walk[];
  selectedId: string;
  onSelect: (id: string) => void;
}

export function HikesMap(props: HikesMapProps) {
  const host = useRef<HTMLDivElement>(null);
  const live = useRef(props);
  live.current = props;
  const sync = useRef<() => void>(() => {});
  const [failed, setFailed] = useState(false);
  const [started, setStarted] = useState(false);
  useEffect(() => {
    let map: maplibregl.Map | undefined;
    let resize: ResizeObserver | undefined;
    let scheme: MediaQueryList | undefined;
    let restyle = () => {};
    let previousPoints = "";
    let previousSelection = "";
    try {
      maplibregl.setWorkerUrl(workerUrl);
      const instance = new maplibregl.Map({
        container: host.current!,
        center: [-4.2, 57],
        zoom: 5.5,
        attributionControl: false,
      });
      map = instance;
      setStarted(true);
      instance.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );
      // Full attribution is also present in the page outside the canvas.
      instance.addControl(
        new maplibregl.AttributionControl({ compact: false }),
      );
      const canvas = instance.getCanvas();
      canvas.setAttribute(
        "aria-label",
        "Hill walks. Arrow keys pan, plus and minus zoom; Enter selects a walk at the centre.",
      );
      canvas.addEventListener("keydown", (event) => {
        if (event.key === "Escape") live.current.onSelect("");
        if (event.key !== "Enter" || !instance.getLayer("walks")) return;
        event.preventDefault();
        const feature = instance.queryRenderedFeatures(
          instance.project(instance.getCenter()),
          { layers: ["walks"] },
        )[0];
        if (feature?.properties.id)
          live.current.onSelect(feature.properties.id);
      });
      instance.on("error", () => setFailed(true));
      instance.on("load", () => {
        instance.addSource("walks", {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        const palette = getComputedStyle(host.current!);
        const colors = Array.from({ length: 5 }, (_, i) =>
          palette.getPropertyValue(`--hikes-effort-${i}`).trim(),
        );
        const tones = () =>
          [...host.current!.querySelectorAll(".hikes-map-tone")].map(
            (probe) => getComputedStyle(probe).color,
          );
        instance.addLayer({
          id: "walks",
          type: "circle",
          source: "walks",
          paint: {
            "circle-color": [
              "interpolate",
              ["linear"],
              ["get", "effort"],
              ...colors.flatMap((color, i) => [i / 4, color]),
            ] as maplibregl.ExpressionSpecification,
            "circle-radius": ["+", 5, ["*", 3, ["get", "effort"]]],
            "circle-stroke-width": 2,
            "circle-stroke-color": tones()[0]!,
          },
        });
        restyle = () => {
          const [sheet, ink, band, stroke] = tones() as [
            string,
            string,
            string,
            string,
          ];
          for (const layer of instance.getStyle().layers) {
            if (layer.id === "walks") continue;
            const paint = (
              name: Parameters<typeof instance.setPaintProperty>[1],
              value: string | number,
            ) => instance.setPaintProperty(layer.id, name, value);
            if (layer.type === "background") paint("background-color", sheet);
            if (layer.type === "fill") {
              paint("fill-color", band);
              if (layer.paint?.["fill-outline-color"])
                paint("fill-outline-color", stroke);
            }
            if (layer.type === "line") paint("line-color", stroke);
            if (layer.type === "fill-extrusion")
              paint("fill-extrusion-color", band);
            if (layer.type === "symbol" && layer.layout?.["text-field"]) {
              paint("text-color", ink);
              paint("text-halo-color", sheet);
            }
            if (layer.type === "raster") {
              paint("raster-saturation", -1);
              paint("raster-opacity", 0.15);
            }
          }
          instance.setPaintProperty("walks", "circle-stroke-color", sheet);
        };
        restyle();
        scheme = window.matchMedia("(prefers-color-scheme: dark)");
        scheme.addEventListener("change", restyle);
        sync.current = () => {
          const { walks, corpus, selectedId } = live.current;
          const ceilings = effortCeilings(corpus);
          const source = instance.getSource("walks") as GeoJSONSource;
          source.setData({
            type: "FeatureCollection",
            features: walks.map((walk) => ({
              type: "Feature",
              properties: { id: walk.id, effort: effort(walk, ceilings) },
              geometry: { type: "Point", coordinates: [walk.lon, walk.lat] },
            })),
          });
          const points = walks.map((walk) => walk.id).join(",");
          if (points !== previousPoints && walks.length) {
            const bounds = new maplibregl.LngLatBounds();
            walks.forEach((walk) => bounds.extend([walk.lon, walk.lat]));
            instance.fitBounds(bounds, {
              padding: 40,
              maxZoom: 11,
              duration: 0,
            });
          }
          previousPoints = points;
          const selected = walks.find((walk) => walk.id === selectedId);
          if (selected && selected.id !== previousSelection)
            instance.easeTo({
              center: [selected.lon, selected.lat],
              zoom: Math.max(9, instance.getZoom()),
              duration: 0,
            });
          previousSelection = selected?.id ?? "";
        };
        instance.on("click", "walks", (event) => {
          const id = event.features?.[0]?.properties.id;
          if (id) live.current.onSelect(id);
        });
        sync.current();
        setFailed(false);
      });
      // A MapLibre basemap definition, separate from DOM inline styles.
      instance.setStyle("https://tiles.openfreemap.org/styles/liberty");
      resize = new ResizeObserver(() => instance.resize());
      resize.observe(host.current!);
    } catch (error) {
      console.error("Could not initialize hikes map", error);
      setFailed(true);
    }
    return () => {
      sync.current = () => {};
      scheme?.removeEventListener("change", restyle);
      resize?.disconnect();
      map?.remove();
    };
  }, []);
  useEffect(
    () => sync.current(),
    [props.walks, props.corpus, props.selectedId],
  );
  return (
    <div>
      {failed && (
        <p role="status">
          The map could not load completely. Choose a walk from the list below.
        </p>
      )}
      <div ref={host} className="hikes-map" hidden={failed && !started}>
        {Array.from({ length: 4 }, (_, i) => (
          <span
            key={i}
            aria-hidden="true"
            className={`hikes-map-tone hikes-tone-${i + 5}`}
          />
        ))}
      </div>
      {!failed && (
        <p className="hikes-legend">
          Effort: gentle{" "}
          <span aria-hidden="true">
            {Array.from({ length: 5 }, (_, i) => (
              <span key={i} className={`hikes-effort-${i}`} />
            ))}
          </span>{" "}
          strenuous
        </p>
      )}
    </div>
  );
}
