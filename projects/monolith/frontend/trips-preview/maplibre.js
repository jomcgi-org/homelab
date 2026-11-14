// Fixture-only access to real maps for browser readiness and error checks.
// The constructor, rendering, camera, sources and controls stay unchanged.
import * as maplibre from "maplibre-gl/dist/maplibre-gl.mjs";
// The real components configure their bundled worker before constructing Map.
export const { Marker, NavigationControl, setWorkerUrl } = maplibre;
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
