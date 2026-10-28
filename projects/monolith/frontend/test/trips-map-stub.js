// A call-recording API double, not a WebGL interaction test. Production map
// components still mount, create layers, register handlers and own cleanup.
import { vi } from "vitest";
export const maps = [];
export class Map {
  constructor(options) {
    this.options = options;
    this.layers = [{ id: "ground", type: "background" }];
    this.sources = {};
    this.handlers = [];
    this.canvas = document.createElement("canvas");
    this.setPaintProperty = vi.fn();
    this.easeTo = vi.fn();
    this.remove = vi.fn();
    this.addControl = vi.fn();
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
    return { layers: this.layers };
  }
  getLayer(id) {
    return this.layers.find((layer) => layer.id === id);
  }
  addSource(id, source) {
    this.sources[id] = source;
  }
  addLayer(layer) {
    this.layers.push(layer);
  }
}
export class NavigationControl {}
export class Marker {
  constructor({ element }) {
    this.element = element;
  }
  setLngLat(coords) {
    this.coords = coords;
    return this;
  }
  getLngLat() {
    return { lng: this.coords[0], lat: this.coords[1] };
  }
  addTo() {
    return this;
  }
  remove() {}
}
