import * as maplibre from "maplibre-gl/dist/maplibre-gl.mjs";

// The preview uses the real renderer/provider. Expose its instance only in
// this fixture so browser assertions can inspect camera, layers and source data.
export class Map extends maplibre.Map {
  constructor(options) {
    super(options);
    window.shipsMap = this;
    window.shipsMapErrors = [];
    this.on("error", (event) =>
      window.shipsMapErrors.push(String(event.error)),
    );
  }
}
export const NavigationControl = maplibre.NavigationControl;
