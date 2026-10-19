import { vi } from "vitest";

export const instances = [];
export class Map {
  constructor({ container }) {
    this.canvas = document.createElement("canvas");
    this.canvas.tabIndex = 0;
    container.append(this.canvas);
    this.handlers = [];
    this.sources = {};
    this.layers = [
      { id: "background", type: "background" },
      { id: "water", type: "fill", "source-layer": "water" },
      { id: "roads", type: "line" },
      { id: "labels", type: "symbol", layout: { "text-field": "name" } },
    ];
    this.setPaintProperty = vi.fn();
    this.setLayoutProperty = vi.fn();
    this.fitBounds = vi.fn();
    this.resize = vi.fn();
    this.remove = vi.fn();
    this.center = { lng: -4.63, lat: 56.386 };
    instances.push(this);
    queueMicrotask(() => this.fire("load"));
  }
  on(event, layer, handler) {
    this.handlers.push({
      event,
      layer: handler ? layer : null,
      handler: handler ?? layer,
    });
  }
  fire(event, detail = {}, layer = null) {
    this.handlers
      .filter((h) => h.event === event && h.layer === layer)
      .forEach((h) => h.handler(detail));
  }
  addControl() {}
  addSource(id, data) {
    this.sources[id] = { ...data, setData: vi.fn() };
  }
  getSource(id) {
    return this.sources[id];
  }
  addLayer(layer) {
    this.layers.push(layer);
  }
  getStyle() {
    return { layers: this.layers };
  }
  getCanvas() {
    return this.canvas;
  }
  getCenter() {
    return this.center;
  }
  project() {
    return { x: 100, y: 100 };
  }
  queryRenderedFeatures() {
    return [{ properties: { id: 5 } }];
  }
}
export class NavigationControl {}
export class LngLatBounds {
  extend() {}
}
