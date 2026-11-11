// Fixture-only access to real maps for browser readiness and error checks.
// The constructor, rendering, camera, sources and controls stay unchanged.
import * as maplibre from "maplibre-gl/dist/maplibre-gl.mjs";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

// MapLibre's dynamic worker URL is not a Vite asset import. Bundle it here so
// the offline preview serves the worker and its imports from the same build.
maplibre.setWorkerUrl(workerUrl);

export const { Marker, NavigationControl } = maplibre;
export class Map extends maplibre.Map {
  constructor(options) {
    super(options);
    this.previewErrors = [];
    this.on("error", (event) => {
      this.previewErrors.push(String(event.error?.message ?? event.error));
    });
    const maps = (globalThis.__tripsPreviewMaps ??= []);
    maps.push(this);
    this.on("remove", () => maps.splice(maps.indexOf(this), 1));
  }
}
