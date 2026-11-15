// A call-recording API double, not a WebGL interaction test. Production map
// components still mount, create layers, register handlers and own cleanup.
import { vi } from "vitest";
export { AJAXError } from "../node_modules/maplibre-gl/src/util/ajax.ts";
const protocols = new globalThis.Map();
export const addProtocol = vi.fn((name, handler) =>
  protocols.set(name, handler),
);
export const removeProtocol = vi.fn((name) => protocols.delete(name));
export const maps = [];
export const markers = [];
export const setWorkerUrl = vi.fn();
export class Map {
  constructor(options) {
    this.workerUrl = setWorkerUrl.mock.calls.at(-1)?.[0];
    this.options = options;
    this.layers = [{ id: "ground", type: "background" }];
    this.sources = {};
    this.handlers = [];
    this.canvas = document.createElement("canvas");
    this.setPaintProperty = vi.fn();
    this.easeTo = vi.fn();
    this.fitBounds = vi.fn();
    this.jumpTo = vi.fn();
    this.remove = vi.fn();
    this.addControl = vi.fn();
    this.setStyle = vi.fn((style) => {
      this.sources = {};
      this.layers = [{ id: "ground", type: "background" }];
      if (typeof style === "object") {
        for (const [id, source] of Object.entries(style.sources)) {
          if (id !== "basemap") this.addSource(id, source);
        }
        this.layers = style.layers;
      }
    });
    maps.push(this);
  }
  on(...args) {
    this.handlers.push(args);
  }
  fire(event, layer, data) {
    for (const args of this.handlers) {
      if (args[0] === event && (args.length === 2 || args[1] === layer))
        args.at(-1)(data);
    }
  }
  getCanvas() {
    return this.canvas;
  }
  getStyle() {
    return {
      layers: this.layers,
      sources: { basemap: { type: "vector" }, ...this.sources },
    };
  }
  getLayer(id) {
    return this.layers.find((layer) => layer.id === id);
  }
  isSourceLoaded() {
    return true;
  }
  // Real MapLibre rejects duplicate IDs when restoring serialized styles.
  addSource(id, source) {
    if (this.getSource(id)) throw new Error(`Source ${id} already exists`);
    source.setData = vi.fn((data) => {
      source.data = data;
    });
    this.sources[id] = source;
  }
  getSource(id) {
    return id === "basemap" ? { type: "vector" } : this.sources[id];
  }
  getCenter() {
    return { lng: -123, lat: 49 };
  }
  getZoom() {
    return 8;
  }
  getBearing() {
    return 10;
  }
  getPitch() {
    return 20;
  }
  addLayer(layer) {
    if (this.getLayer(layer.id))
      throw new Error(`Layer ${layer.id} already exists`);
    this.layers.push(layer);
  }
  fetchTile(url, parameters = {}, controller = new AbortController()) {
    const request = {
      ...this.options.transformRequest(url, "Tile"),
      ...parameters,
    };
    const protocol = new URL(request.url).protocol.slice(0, -1);
    return protocols.get(protocol)(request, controller);
  }
  loseContext() {
    // MapLibre saves all custom sources/layers, then destroys the style.
    this.lostStyle = JSON.parse(JSON.stringify(this.getStyle()));
    this.sources = {};
    this.layers = [];
    this.fire("webglcontextlost", undefined, {});
  }
  restoreContext() {
    // _contextRestored sets the serialized style before announcing recovery.
    this.setStyle(this.lostStyle, { diff: false });
    this.fire("webglcontextrestored", undefined, {});
  }
}
export class NavigationControl {}
export class Marker {
  constructor({ element }) {
    this.element = element;
    this.remove = vi.fn();
    markers.push(this);
  }
  setLngLat(coords) {
    this.coords = coords;
    return this;
  }
  getLngLat() {
    return { lng: this.coords[0], lat: this.coords[1] };
  }
  getElement() {
    return this.element;
  }
  addTo() {
    return this;
  }
}
