// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import { dates, snapshot } from "../../../../../campsites-preview/fixtures.js";

vi.mock("$app/environment", () => ({ browser: true, version: "testbuild" }));
const { invalidateAll, maps, popups, setWorkerUrl } = vi.hoisted(() => ({
  invalidateAll: vi.fn(),
  setWorkerUrl: vi.fn(),
  maps: [],
  popups: [],
}));
vi.mock("$app/navigation", () => ({ invalidateAll }));
// Asset transformation belongs to Vite's production build, not the WebGL DOM
// double. Bazel's runfiles root cannot serve the package's ?url import. Keep
// this seam exact; browser-check.mjs verifies the real emitted worker loads.
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/test-assets/maplibre-worker.js",
}));
// Only external WebGL and its worker URL are mocked. Page, map, theme paint,
// scheme switch and every event/effect are the production components.
vi.mock("maplibre-gl", () => ({
  setWorkerUrl,
  Map: class {
    constructor(options) {
      expect(setWorkerUrl).toHaveBeenLastCalledWith(
        "/test-assets/maplibre-worker.js",
      );
      this.options = options;
      this.events = [];
      this.sources = {};
      this.layers = [
        { id: "background", type: "background" },
        { id: "water", type: "fill" },
        { id: "road", type: "line" },
        { id: "label", type: "symbol", layout: { "text-field": "name" } },
      ];
      this.canvas = document.createElement("canvas");
      this.setPaintProperty = vi.fn();
      this.flyTo = vi.fn();
      this.fitBounds = vi.fn();
      this.resize = vi.fn();
      this.remove = vi.fn(() => {
        this.events = [];
      });
      this.hits = [];
      this.queryRenderedFeatures = vi.fn(() => this.hits);
      maps.push(this);
    }
    on(event, layerOrFn, fn) {
      this.events.push({ event, fn: fn ?? layerOrFn });
    }
    off(event, fn) {
      this.events = this.events.filter((e) => e.event !== event || e.fn !== fn);
    }
    emit(event, data) {
      for (const e of this.events.filter((e) => e.event === event)) e.fn(data);
    }
    addControl() {}
    addSource(id, source) {
      this.sources[id] = {
        ...source,
        setData: vi.fn((data) => {
          this.sources[id].data = data;
        }),
      };
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
    setLayoutProperty(id, key, value) {
      const layer = this.layers.find((layer) => layer.id === id);
      layer.layout = { ...layer.layout, [key]: value };
    }
    getLayoutProperty(id, key) {
      return this.layers.find((layer) => layer.id === id).layout?.[key];
    }
    getZoom() {
      return 5;
    }
    getCanvas() {
      return this.canvas;
    }
    project(coordinates) {
      return { x: coordinates[0], y: coordinates[1] };
    }
  },
  NavigationControl: class {},
  Popup: class {
    constructor() {
      this.remove = vi.fn();
      popups.push(this);
    }
    setLngLat() {
      return this;
    }
    setHTML(html) {
      this.html = html;
      return this;
    }
    addTo() {
      return this;
    }
  },
}));

let instance, target, resizeDisconnect, media;
beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  history.replaceState({}, "", "/app/campsites");
  delete document.documentElement.dataset.theme;
  localStorage.clear();
  maps.length = 0;
  popups.length = 0;
  invalidateAll.mockClear();
  setWorkerUrl.mockClear();
  media = new EventTarget();
  media.matches = false;
  vi.spyOn(media, "addEventListener");
  vi.spyOn(media, "removeEventListener");
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => media),
  );
  resizeDisconnect = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {
        resizeDisconnect();
      }
    },
  );
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render(data = { snapshot }) {
  instance = mount(Page, { target, props: { data } });
  await tick();
  await vi.waitFor(() => expect(maps).toHaveLength(1));
  maps[0].emit("load");
  await tick();
  return maps[0];
}
async function click(selector) {
  target.querySelector(selector).click();
  await tick();
}
const names = () =>
  [...target.querySelectorAll(".r-name")].map((e) => e.textContent);
const features = () => maps[0].getSource("parks").data.features;

describe("campsites production page and map boundary", () => {
  it("renders one sheet, existing metadata, initial collapsed controls and every park", async () => {
    const map = await render();
    expect(setWorkerUrl).toHaveBeenCalledTimes(1);
    expect(target.querySelector("main.td.sheet")).not.toBeNull();
    expect(target.querySelectorAll("button.scheme")).toHaveLength(1);
    expect(
      target.querySelector('.spine nav[aria-label="Site"]'),
    ).not.toBeNull();
    expect(
      target.querySelector(".nights-toggle").getAttribute("aria-expanded"),
    ).toBe("false");
    expect(target.querySelector(".rows")).toBeNull();
    expect(document.title).toBe(
      "BC Parks campsites, open sites and clear-sky weather",
    );
    expect(features().map((f) => f.properties.id)).toEqual([
      "birch",
      "alpha",
      "cedar",
      "dormant",
    ]);
    expect(map.layers.map((l) => l.id).slice(-2)).toEqual([
      "weather-heat",
      "parks",
    ]);
    expect(map.fitBounds).toHaveBeenCalledTimes(1);
    await click(".collapse-btn");
    expect(names()).toEqual([
      "Birch Lake",
      "Alpha Lake",
      "Cedar Lake",
      "Dormant Lake",
    ]);
  });

  it("hydrates night URLs, drops dead nights, re-scores and preserves unrelated query state", async () => {
    history.replaceState(
      { keep: 1 },
      "",
      `/app/campsites?other=keep&nights=${dates[1]},${dates[3]}`,
    );
    await render();
    expect(
      features().map((f) => [f.properties.id, f.properties.best_score]),
    ).toEqual([
      ["alpha", 80],
      ["birch", 30],
    ]);
    expect(target.querySelector(".nights-toggle-summary").textContent).toBe(
      "1 night",
    );
    await click(".nights-toggle");
    const buttons = target.querySelectorAll(".night-btn");
    expect(buttons[3].disabled).toBe(true);
    expect(buttons[3].getAttribute("aria-pressed")).toBe("false");
    await click(".nights-act");
    expect(new URL(location.href).searchParams.has("nights")).toBe(false);
    expect(new URL(location.href).searchParams.get("other")).toBe("keep");
    expect(history.state).toEqual({ keep: 1 });
    await click(".night-btn");
    expect(new URL(location.href).searchParams.get("nights")).toBe(
      `${dates[1]},${dates[2]}`,
    );
    expect(features().map((f) => f.properties.id)).toEqual([
      "alpha",
      "birch",
      "cedar",
    ]);
    await click(".nights-act:nth-child(3)");
    expect(new URL(location.href).searchParams.get("nights")).toBe("");
    expect(features()).toHaveLength(0);
    expect(target.querySelector(".nights-hint")).not.toBeNull();
    await click(".nights-act:nth-child(2)");
    expect(new URL(location.href).searchParams.has("nights")).toBe(false);
    expect(features()).toHaveLength(4);
  });

  it.each(["", dates[3]])(
    "preserves an explicit no-night or dead-night URL (%s)",
    async (nights) => {
      history.replaceState({}, "", `/app/campsites?nights=${nights}`);
      await render();
      expect(features()).toHaveLength(0);
      await click(".collapse-btn");
      expect(target.querySelector(".empty").textContent).toContain(
        "No parks match",
      );
      expect(target.querySelector(".nights-toggle-summary").textContent).toBe(
        "No nights",
      );
    },
  );

  it("executes all three sorts and the clear-only filter", async () => {
    await render();
    await click(".collapse-btn");
    await click(".seg:nth-child(2)");
    expect(names()).toEqual([
      "Alpha Lake",
      "Birch Lake",
      "Cedar Lake",
      "Dormant Lake",
    ]);
    await click(".seg:nth-child(1)");
    expect(names()[0]).toBe("Birch Lake");
    await click(".seg:nth-child(3)");
    expect(names()).toEqual([
      "Alpha Lake",
      "Birch Lake",
      "Cedar Lake",
      "Dormant Lake",
    ]);
    await click(".check-input");
    expect(names()).toEqual(["Alpha Lake", "Birch Lake"]);
    expect(features()).toHaveLength(2);
  });

  it("links list selection, map hit testing, forecast detail and background deselection", async () => {
    const map = await render();
    await click(".collapse-btn");
    await click(".row");
    expect(target.querySelector(".detail-park").textContent).toBe("Birch Lake");
    expect(features().find((f) => f.id === "birch").properties.sel).toBe(1);
    expect(map.flyTo).toHaveBeenLastCalledWith(
      expect.objectContaining({ center: [-122.1, 50.9] }),
    );
    expect(target.querySelectorAll(".day-cell")).toHaveLength(14);
    expect(target.querySelector(".day-cell").title).toBe(
      "Open, 3% cloud, 0.0 mm rain, 16C max",
    );
    expect(target.querySelector(".day-temp").textContent).toBe("16°");
    expect(target.querySelector(".day-rain-wet").textContent).toBe("2.4mm");
    expect(target.querySelector(".book-link").href).toBe(
      "https://example.com/booking",
    );
    map.hits = [features().find((f) => f.id === "alpha")];
    map.emit("click", { point: { x: -123.8, y: 49.8 } });
    await tick();
    expect(map.queryRenderedFeatures).toHaveBeenCalledWith(
      [
        [-137.8, 35.8],
        [-109.8, 63.8],
      ],
      { layers: ["parks"] },
    );
    expect(target.querySelector(".row.selected .r-name").textContent).toBe(
      "Alpha Lake",
    );
    expect(popups[0].html).toContain("Alpha Lake");
    await click(".detail-close");
    expect(target.querySelector(".detail")).toBeNull();
    map.hits = [];
    map.emit("click", { point: { x: 0, y: 0 } });
    await tick();
    expect(features().every((f) => f.properties.sel === 0)).toBe(true);
    expect(popups[0].remove).toHaveBeenCalled();
  });

  it("repaints theme without replacing sources, layers, selection or listeners", async () => {
    const map = await render();
    await click(".collapse-btn");
    await click(".row");
    map.setLayoutProperty("weather-heat", "visibility", "none");
    const source = map.getSource("parks");
    const layers = [...map.layers];
    const listeners = [...map.events];
    const cameraCalls = map.flyTo.mock.calls.length;
    map.setPaintProperty.mockClear();
    await click("button.scheme");
    await vi.waitFor(() => expect(map.setPaintProperty).toHaveBeenCalled());
    expect(localStorage.getItem("td-theme")).toBe("dark");
    expect(map.getSource("parks")).toBe(source);
    expect(map.layers).toEqual(layers);
    expect(map.events).toEqual(listeners);
    expect(map.getLayoutProperty("weather-heat", "visibility")).toBe("none");
    expect(map.flyTo).toHaveBeenCalledTimes(cameraCalls);
    expect(maps).toHaveLength(1);
    expect(features().find((f) => f.id === "birch").properties.sel).toBe(1);
    expect(
      map.setPaintProperty.mock.calls.some(
        ([id, key]) => id === "parks" && key === "circle-color",
      ),
    ).toBe(true);
    expect(
      map.setPaintProperty.mock.calls.some(
        ([id, key]) => id === "water" && key === "fill-color",
      ),
    ).toBe(true);
  });

  it("follows OS scheme changes and unregisters the media listener on unmount", async () => {
    const map = await render();
    const source = map.getSource("parks");
    const listeners = [...map.events];
    map.setLayoutProperty("weather-heat", "visibility", "none");
    map.setPaintProperty.mockClear();
    media.matches = true;
    media.dispatchEvent(new Event("change"));
    await tick();
    expect(
      target.querySelector("button.scheme").getAttribute("aria-label"),
    ).toBe("Switch to day scheme");
    expect(map.setPaintProperty).toHaveBeenCalledWith(
      "parks",
      "circle-color",
      expect.any(Array),
    );
    expect(map.getSource("parks")).toBe(source);
    expect(map.events).toEqual(listeners);
    expect(map.getLayoutProperty("weather-heat", "visibility")).toBe("none");
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem("td-theme")).toBeNull();
    const callbacks = media.addEventListener.mock.calls.filter(
      ([type]) => type === "change",
    );
    await unmount(instance);
    instance = undefined;
    for (const [type, listener] of callbacks) {
      expect(media.removeEventListener).toHaveBeenCalledWith(type, listener);
    }
    map.setPaintProperty.mockClear();
    media.matches = false;
    media.dispatchEvent(new Event("change"));
    await tick();
    expect(map.setPaintProperty).not.toHaveBeenCalled();
  });

  it("ignores scheme changes before map load and initializes once with current selection", async () => {
    instance = mount(Page, { target, props: { data: { snapshot } } });
    await tick();
    await vi.waitFor(() => expect(maps).toHaveLength(1));
    const map = maps[0];
    await click("button.scheme");
    await click(".collapse-btn");
    await click(".row");
    media.dispatchEvent(new Event("change"));
    await tick();
    expect(map.setPaintProperty).not.toHaveBeenCalled();
    expect(map.flyTo).not.toHaveBeenCalled();
    map.emit("load");
    await tick();
    expect(features().find((f) => f.id === "birch").properties.sel).toBe(1);
    expect(map.flyTo).toHaveBeenCalledTimes(1);
    expect(map.setPaintProperty).toHaveBeenCalled();
  });

  it("closes the legend outside, keeps attribution open once requested and cleans up", async () => {
    const remove = vi.spyOn(document, "removeEventListener");
    const interval = vi.spyOn(globalThis, "setInterval");
    const clear = vi.spyOn(globalThis, "clearInterval");
    const map = await render();
    const poll = interval.mock.calls.findIndex(
      ([, delay]) => delay === 15 * 60_000,
    );
    interval.mock.calls[poll][0]();
    expect(invalidateAll).toHaveBeenCalledTimes(1);
    await click(".legend-toggle");
    expect(target.querySelector(".legend-open")).not.toBeNull();
    document.body.dispatchEvent(
      new PointerEvent("pointerdown", { bubbles: true }),
    );
    await tick();
    expect(target.querySelector(".legend-open")).toBeNull();
    const attribution = document.createElement("details");
    attribution.className = "maplibregl-ctrl-attrib maplibregl-compact-show";
    attribution.open = false;
    const button = document.createElement("button");
    button.className = "maplibregl-ctrl-attrib-button";
    attribution.append(button);
    target.querySelector(".map").append(attribution);
    button.click();
    map.emit("sourcedata");
    expect(attribution.open).toBe(true);
    await unmount(instance);
    instance = undefined;
    expect(clear).toHaveBeenCalledWith(interval.mock.results[poll].value);
    expect(resizeDisconnect).toHaveBeenCalledTimes(1);
    expect(map.remove).toHaveBeenCalledTimes(1);
    expect(map.events).toHaveLength(0);
    expect(remove.mock.calls.some(([type]) => type === "pointerdown")).toBe(
      true,
    );
    map.setPaintProperty.mockClear();
    document.documentElement.dataset.theme = "dark";
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(map.setPaintProperty).not.toHaveBeenCalled();
  });

  it("renders an empty snapshot without fitting an empty map", async () => {
    const map = await render({ snapshot: null });
    expect(features()).toHaveLength(0);
    expect(map.fitBounds).not.toHaveBeenCalled();
    await click(".collapse-btn");
    expect(target.querySelector(".empty")).not.toBeNull();
    await click(".nights-toggle");
    expect(target.querySelectorAll(".night-btn")).toHaveLength(0);
  });

  it("retains all parks at the default when every night is dead", async () => {
    await render({ snapshot: { ...snapshot, parks: [snapshot.parks[3]] } });
    expect(features().map((f) => f.properties.id)).toEqual(["dormant"]);
    await click(".nights-toggle");
    expect(target.querySelectorAll(".night-btn:disabled")).toHaveLength(14);
    await click(".nights-act:nth-child(3)");
    expect(target.querySelector(".nights-toggle-summary").textContent).toBe(
      "All nights",
    );
    expect(new URL(location.href).searchParams.has("nights")).toBe(false);
    await click(".collapse-btn");
    await click(".check-input");
    expect(features()).toHaveLength(0);
  });
});
