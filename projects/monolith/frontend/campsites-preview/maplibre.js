// Observe the real library, never replace its renderer or events. This preview
// handle lets browser checks assert sources, layers and camera across schemes.
import * as real from "maplibre-gl/dist/maplibre-gl.mjs";
export * from "maplibre-gl/dist/maplibre-gl.mjs";
export class Map extends real.Map {
  constructor(options) {
    super(options);
    window.campsitesMap = this;
  }
}
