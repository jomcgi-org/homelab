import { vi } from "vitest";

// Narrow MapLibre double: enforce source/layer/image identity and fail writes
// after remove. Rendering and provider loading are checked in the real browser.
export const maps = [];
export const setWorkerUrl = vi.fn();
export class Map {
  constructor(options) {
    this.options = options;
    this.sources = new globalThis.Map();
    this.images = new globalThis.Map();
    this.layers = new globalThis.Map([
      ["background", { id: "background", type: "background", paint: {} }],
      ["water", { id: "water", type: "fill", paint: {} }],
      [
        "label",
        {
          id: "label",
          type: "symbol",
          layout: { "text-field": "name" },
          paint: {},
        },
      ],
    ]);
    this.listeners = [];
    this.canvas = document.createElement("canvas");
    this.camera = { center: [1, 2], zoom: 8 };
    this.removed = false;
    for (const name of [
      "addSource",
      "addLayer",
      "addImage",
      "updateImage",
      "setPaintProperty",
      "setLayoutProperty",
      "setFilter",
      "remove",
      "fitBounds",
    ]) {
      this[name] = vi.fn(this[name].bind(this));
    }
    maps.push(this);
  }
  assertLive() {
    if (this.removed) throw new Error("write after map removal");
  }
  on(event, layer, handler) {
    this.listeners.push({
      event,
      layer: handler ? layer : null,
      handler: handler ?? layer,
    });
  }
  emit(event, data = {}, layer = null) {
    for (const l of this.listeners)
      if (l.event === event && l.layer === layer) l.handler(data);
  }
  addControl() {}
  getCanvas() {
    return this.canvas;
  }
  getStyle() {
    return { layers: [...this.layers.values()] };
  }
  getSource(id) {
    this.assertLive();
    return this.sources.get(id);
  }
  addSource(id, source) {
    this.assertLive();
    if (this.sources.has(id)) throw new Error(`duplicate source ${id}`);
    const stored = {
      ...source,
      setData: vi.fn((data) => {
        this.assertLive();
        stored.data = data;
      }),
    };
    this.sources.set(id, stored);
  }
  addLayer(layer) {
    this.assertLive();
    if (this.layers.has(layer.id))
      throw new Error(`duplicate layer ${layer.id}`);
    this.layers.set(layer.id, structuredClone(layer));
  }
  addImage(id, image) {
    this.assertLive();
    if (this.images.has(id)) throw new Error(`duplicate image ${id}`);
    this.images.set(id, image);
  }
  updateImage(id, image) {
    this.assertLive();
    if (!this.images.has(id)) throw new Error(`missing image ${id}`);
    this.images.set(id, image);
  }
  hasImage(id) {
    return this.images.has(id);
  }
  setPaintProperty(id, key, value) {
    this.assertLive();
    this.layers.get(id).paint[key] = value;
  }
  setLayoutProperty(id, key, value) {
    this.assertLive();
    this.layers.get(id).layout[key] = value;
  }
  setFilter(id, filter) {
    this.assertLive();
    this.layers.get(id).filter = filter;
  }
  fitBounds(bounds) {
    this.camera = { bounds };
  }
  remove() {
    this.assertLive();
    this.removed = true;
    this.listeners = [];
  }
}
export class NavigationControl {}
