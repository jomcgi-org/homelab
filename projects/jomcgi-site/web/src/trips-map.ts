// Only imported after the map button is activated. No style or tile request
// occurs before intent. GeoJSON stays an object, never a data: URL.
import * as maplibregl from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Day } from "./trips-types";

export function showMap(container: HTMLElement, days: Day[], slug: string) {
  maplibregl.setWorkerUrl(workerUrl);
  const map = new maplibregl.Map({
    container,
    center: [0, 0],
    zoom: 1,
  });
  map.setStyle("https://tiles.openfreemap.org/styles/liberty");
  map.addControl(new maplibregl.NavigationControl());
  map.on("load", () => {
    map.resize();
    const color = getComputedStyle(container).color;
    const features = days
      .filter((day) => day.points.length > 1)
      .map((day) => ({
        type: "Feature" as const,
        properties: { day: day.dayNumber },
        geometry: {
          type: "LineString" as const,
          coordinates: day.points.map((p) => [p.lng, p.lat]),
        },
      }));
    map.addSource("route", {
      type: "geojson",
      data: { type: "FeatureCollection", features },
    });
    map.addLayer({
      id: "route",
      type: "line",
      source: "route",
      paint: { "line-color": color, "line-width": 4 },
    });
    const bounds = new maplibregl.LngLatBounds();
    for (const day of days)
      for (const p of day.points) bounds.extend([p.lng, p.lat]);
    if (!bounds.isEmpty())
      map.fitBounds(bounds, { padding: 40, maxZoom: 13, duration: 0 });
    map.on("click", "route", (event) => {
      const day = Number(event.features?.[0]?.properties?.day);
      if (Number.isInteger(day) && day > 0)
        window.location.assign(`/trips/${encodeURIComponent(slug)}/day/${day}`);
    });
  });
  return () => map.remove();
}
