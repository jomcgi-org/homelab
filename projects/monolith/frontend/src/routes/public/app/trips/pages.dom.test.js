// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { fromStore, writable } from "svelte/store";
import Route from "../../../../../test/TripsRoute.svelte";
import { tripsData } from "../../../../../test/trips-fixtures.js";
import {
  maps,
  markers,
  setWorkerUrl,
} from "../../../../../test/trips-map-stub.js";
import { setPage } from "../../../../../test/profile-page.svelte.js";
vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
vi.mock("maplibre-gl", () => import("../../../../../test/trips-map-stub.js"));
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/trips-test-worker.js",
}));
vi.mock("$app/navigation", () => ({ goto: vi.fn() }));

let instance;
let dataStore;
const settle = async () => {
  for (let i = 0; i < 6; i++) await tick();
};
const button = (name) => document.querySelector(`button[aria-label="${name}"]`);
const textButton = (text) =>
  [...document.querySelectorAll("button")].find(
    (b) => b.textContent.trim() === text,
  );
beforeEach(() => {
  maps.length = 0;
  markers.length = 0;
  setWorkerUrl.mockClear();
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  await settle();
  document.body.innerHTML = "";
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render(surface, data = tripsData(), query = "") {
  const path = {
    index: "",
    trip: "/fixture-walk",
    day: "/fixture-walk/day/1",
    timeline: "/fixture-walk/timeline",
  }[surface];
  setPage(`https://jomcgi.dev/app/trips${path}${query}`, data);
  dataStore = writable(data);
  const reactive = fromStore(dataStore);
  instance = mount(Route, {
    target: document.body,
    props: {
      surface,
      get data() {
        return reactive.current;
      },
    },
  });
  await settle();
  await vi.waitFor(() => {
    if (surface === "trip" || surface === "day" || query.includes("view=map"))
      expect(maps).toHaveLength(1);
  });
  maps.forEach((map) => map.fire("load"));
  await settle();
  maps.forEach((map) => map.fire("render"));
  await settle();
}

describe("trips composed sheets", () => {
  it.each(["trip", "day", "timeline"])(
    "configures the bundled worker before constructing the %s map",
    async (surface) => {
      await render(
        surface,
        tripsData(),
        surface === "timeline" ? "?view=map" : "",
      );
      expect(maps[0].workerUrl).toBe("/trips-test-worker.js");
      expect(setWorkerUrl).toHaveBeenCalledOnce();
    },
  );
  it.each(["index", "trip", "day", "timeline"])(
    "mounts the real %s wrapper with one chrome and a working scheme switch",
    async (surface) => {
      await render(surface);
      expect(document.querySelectorAll("main.td.sheet")).toHaveLength(1);
      expect(document.querySelectorAll(".chrome")).toHaveLength(1);
      expect(document.querySelectorAll("button.scheme")).toHaveLength(1);
      expect(document.querySelector(".md-nav")).toBeNull();
      expect(document.querySelector('nav[aria-label="Site"]')).not.toBeNull();
      document.querySelector("button.scheme").click();
      await settle();
      expect(document.documentElement.dataset.theme).toBe("dark");
      await window.happyDOM.waitUntilComplete();
      expect(localStorage.getItem("td-theme")).toBe("dark");
    },
  );
  it("preserves index cover URLs, trip links and the empty state", async () => {
    const data = tripsData();
    await render("index", data);
    expect(document.querySelector("h1").textContent).toBe("Trips");
    expect(document.querySelector("a.card").getAttribute("href")).toBe(
      "/app/trips/fixture-walk",
    );
    expect(document.querySelector("a.card img").getAttribute("src")).toBe(
      data.trip.coverUrl,
    );
    dataStore.set({ ...data, index: { trips: [] } });
    await settle();
    expect(document.body.textContent).toContain("No trips yet.");
  });
  it.each(["day", "timeline"])(
    "keeps the %s trip link in its labelled mobile index",
    async (surface) => {
      const data = tripsData();
      await render(surface, data);
      const index = document.querySelector('nav[aria-label="Trip"]');
      expect(index.classList.contains("strip")).toBe(true);
      expect(index.querySelector(".sec-label").textContent).toBe("/ Index");
      const links = index.querySelectorAll(
        `a[href="/app/trips/${data.trip.slug}"]`,
      );
      expect(links).toHaveLength(1);
      expect(links[0].textContent).toBe(data.trip.short_title);
      if (surface === "timeline") {
        expect(
          document.querySelectorAll(`a[href="/app/trips/${data.trip.slug}"]`),
        ).toHaveLength(1);
      }
    },
  );
  it("retains summary totals, export controls, route selection and empty data", async () => {
    await render("trip");
    expect(document.querySelector("h1").textContent).toBe(
      tripsData().trip.title,
    );
    expect(document.querySelector('a[href$="/timeline"]')).not.toBeNull();
    expect(textButton("↓ GPX")).not.toBeUndefined();
    expect(textButton("↓ JSON")).not.toBeUndefined();
    const map = maps[0];
    map.fire("mouseenter", "route-hit-1");
    await settle();
    expect(document.querySelectorAll(".dayrow.active")).toHaveLength(1);
    expect(map.setPaintProperty).toHaveBeenCalledWith(
      "route-0",
      "line-opacity",
      0.15,
    );
    map.fire("click", "route-hit-1");
    expect(window.location.pathname).toBe("/app/trips/fixture-walk/day/2");
    dataStore.set({ ...tripsData(), points: [] });
    await settle();
    expect(document.body.textContent).toContain("No points for this trip yet.");
  });
  it("keeps day navigation, bounded scrubber, fullscreen dismissal and day reset", async () => {
    await render("day");
    expect(document.querySelector('a[href$="/day/2"]')).not.toBeNull();
    expect(document.querySelector('a[href$="/day/0"]')).toBeNull();
    expect(button("Previous photo").disabled).toBe(true);
    button("Next photo").click();
    await settle();
    button("View photo 2 fullscreen").click();
    await settle();
    expect(
      button("Close fullscreen photo").querySelector("img").getAttribute("src"),
    ).toBe(tripsData().points[1].imgGallery);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await settle();
    expect(button("Close fullscreen photo")).toBeNull();
    maps[0].fire("click", "route-hit-area", {
      lngLat: { lng: -122.9, lat: 49.08 },
    });
    await settle();
    expect(button("Next photo").disabled).toBe(true);
    dataStore.set({ ...tripsData(), dayNumber: 2 });
    await settle();
    expect(button("Previous photo").disabled).toBe(true);
    expect(button("View photo 1 fullscreen")).not.toBeNull();
    dataStore.set({ ...tripsData(), dayNumber: 9 });
    await settle();
    expect(document.body.textContent).toContain("Day 9 not found.");
    expect(document.querySelector(".missing a").getAttribute("href")).toBe(
      "/app/trips/fixture-walk",
    );
  });
  it("updates the existing map route when the selected day changes", async () => {
    const data = tripsData();
    await render("day", data);
    const map = maps[0];
    const coordinates = (points) =>
      points.map((point) => [point.lng, point.lat]);
    expect(map.sources.route.data.geometry.coordinates).toEqual(
      coordinates(data.points.slice(0, 3)),
    );
    const source = map.sources.route;
    const layers = map.layers.map((layer) => layer.id);
    const start = markers.find(
      (marker) => marker.element.style.width === "16px",
    );
    const end = markers.find((marker) => marker.element.style.width === "14px");
    const photoMarker = markers.find(
      (marker) => marker.element.style.width === "24px",
    );
    const firstPhotoColor = photoMarker.getElement().style.background;
    dataStore.set({ ...data, dayNumber: 2 });
    await settle();
    expect(maps).toHaveLength(1);
    expect(map.remove).not.toHaveBeenCalled();
    expect(map.sources.route.data.geometry.coordinates).toEqual(
      coordinates(data.points.slice(3)),
    );
    expect(map.sources.route).toBe(source);
    expect(map.layers.map((layer) => layer.id)).toEqual(layers);
    expect(start.coords).toEqual(coordinates(data.points.slice(3))[0]);
    expect(end.coords).toEqual(coordinates(data.points.slice(3)).at(-1));
    expect(map.setPaintProperty).toHaveBeenCalledWith(
      "route-color-accent",
      "line-color",
      "#059669",
    );
    expect(button("View photo 1 fullscreen")).not.toBeNull();
    expect(photoMarker.getElement().style.background).not.toBe(firstPhotoColor);
    const expectedColor = document.createElement("div");
    expectedColor.style.background = "#059669";
    expect(photoMarker.getElement().style.background).toBe(
      expectedColor.style.background,
    );
    const routeUpdates = source.setData.mock.calls.length;
    map.fire("click", "route-hit-area", {
      lngLat: { lng: data.points[5].lng, lat: data.points[5].lat },
    });
    await settle();
    expect(button("Next photo").disabled).toBe(true);
    expect(source.setData).toHaveBeenCalledTimes(routeUpdates);
    dataStore.set({ ...data, dayNumber: 1 });
    await settle();
    expect(map.sources.route.data.geometry.coordinates).toEqual(
      coordinates(data.points.slice(0, 3)),
    );
    expect(button("View photo 1 fullscreen")).not.toBeNull();
    expect(photoMarker.getElement().style.background).toBe(firstPhotoColor);
    await unmount(instance);
    instance = null;
    expect(start.remove).toHaveBeenCalledOnce();
    expect(end.remove).toHaveBeenCalledOnce();
  });

  it("updates a one-point day after a refreshed route arrives", async () => {
    const data = tripsData();
    await render("day", { ...data, points: [data.points[0]] });
    const map = maps[0];
    expect(map.sources.route.data).toEqual({
      type: "FeatureCollection",
      features: [],
    });
    dataStore.set(data);
    await settle();
    expect(map.sources.route.data.geometry.coordinates).toEqual(
      data.points.slice(0, 3).map((point) => [point.lng, point.lat]),
    );
    dataStore.set({ ...data, points: [data.points[0]] });
    await settle();
    expect(map.sources.route.data).toEqual({
      type: "FeatureCollection",
      features: [],
    });
    expect(maps).toHaveLength(1);
  });

  it("keeps timeline URL parameters, filtering and the real view control", async () => {
    await render("timeline", tripsData(), "?view=map&keep=1");
    expect(textButton("Map").getAttribute("aria-pressed")).toBe("true");
    textButton("Photos").click();
    await settle();
    const { goto } = await import("$app/navigation");
    const [url, options] = goto.mock.calls.at(-1);
    expect(url.search).toBe("?keep=1");
    expect(options).toEqual({
      keepFocus: true,
      noScroll: true,
      replaceState: true,
    });
    textButton("trail").click();
    await settle();
    expect(document.body.textContent).toContain("3 / 6 photos");
    button("Open photo 2").click();
    await settle();
    expect(
      document.querySelector('[role="dialog"] img').getAttribute("src"),
    ).toBe(tripsData().points[2].imgDisplay);
  });
  it.each(["trip", "day"])(
    "repaints the existing %s map and cleans up scheme listeners",
    async (surface) => {
      // Happy DOM 20.8.9 keeps MutationObserverListener's callback only in a
      // WeakRef. GC during the full suite can silently drop that callback.
      // Retain it for this test so the real observer, toggle and paint path
      // still run, including disconnect on unmount. Browsers do not need this.
      vi.stubGlobal(
        "WeakRef",
        class extends WeakRef {
          constructor(target) {
            super(target);
            this.retained = target;
          }
        },
      );
      await render(surface);
      const map = maps[0];
      const layers = map.layers.map((l) => l.id);
      const sources = { ...map.sources };
      const cameraCalls = map.easeTo.mock.calls.length;
      const original = window.getComputedStyle;
      vi.spyOn(window, "getComputedStyle").mockImplementation((el) => {
        if (!el.classList.contains("map")) return original(el);
        return {
          getPropertyValue: (name) =>
            `${document.documentElement.dataset.theme || "light"}:${name}`,
        };
      });
      document.querySelector("button.scheme").click();
      await settle();
      expect(document.documentElement.dataset.theme).toBe("dark");
      await window.happyDOM.waitUntilComplete();
      await vi.waitFor(() =>
        expect(map.setPaintProperty).toHaveBeenCalledWith(
          "ground",
          "background-color",
          "dark:--sheet",
        ),
      );
      expect(maps).toHaveLength(1);
      expect(map.layers.map((l) => l.id)).toEqual(layers);
      expect(map.sources).toEqual(sources);
      expect(map.easeTo.mock.calls).toHaveLength(cameraCalls);
      expect(map.remove).not.toHaveBeenCalled();
      await unmount(instance);
      instance = null;
      const paintCalls = map.setPaintProperty.mock.calls.length;
      document.documentElement.dataset.theme = "light";
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(map.setPaintProperty.mock.calls).toHaveLength(paintCalls);
      expect(map.remove).toHaveBeenCalledOnce();
    },
  );
});

function photoRequests() {
  const requested = [];
  vi.stubGlobal(
    "Image",
    class {
      complete = false;
      naturalWidth = 0;
      fetchPriority = "auto";
      set src(value) {
        this.url = value;
        requested.push(this);
      }
      get src() {
        return this.url;
      }
      decode() {
        return new Promise((resolve, reject) => {
          this.decoded = resolve;
          this.failed = reject;
        });
      }
      removeAttribute(name) {
        if (name === "src") this.cancelled = true;
      }
    },
  );
  return requested;
}
function galleryData() {
  const data = tripsData();
  data.points = Array.from({ length: 30 }, (_, i) => ({
    ...data.points[0],
    id: `warm-${i}`,
    lat: 49 + i * 0.001,
    lng: -123 + i * 0.001,
    taken_at: new Date(
      Date.parse("2026-10-17T16:00:00Z") + i * 60000,
    ).toISOString(),
    imgDisplay: `/fixture-display-${i}.jpg`,
  }));
  return data;
}
async function mountDay(data) {
  setPage("https://jomcgi.dev/app/trips/fixture-walk/day/1", data);
  instance = mount(Route, {
    target: document.body,
    props: { surface: "day", data },
  });
  await settle();
  await vi.waitFor(() => expect(maps).toHaveLength(1));
  return maps[0];
}

describe("trip image loading", () => {
  it("prioritizes only the first index cover without changing image URLs", async () => {
    const data = tripsData();
    data.index.trips.push({
      ...data.trip,
      slug: "second-trip",
      coverUrl: "/second-cover.jpg",
    });
    await render("index", data);
    const [first, second] = document.querySelectorAll("a.card img");
    expect(first.getAttribute("src")).toBe(data.trip.coverUrl);
    expect(first.getAttribute("loading")).toBe("eager");
    expect(first.getAttribute("fetchpriority")).toBe("high");
    expect(second.getAttribute("src")).toBe("/second-cover.jpg");
    expect(second.getAttribute("loading")).toBe("lazy");
    expect(second.getAttribute("fetchpriority")).toBe("auto");
  });

  it.each(["photo", "map"])(
    "waits for the photo and populated map when %s is ready first",
    async (first) => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      const requested = photoRequests();
      const map = await mountDay(galleryData());
      expect(requested.map((image) => image.src)).toEqual([
        "/fixture-display-0.jpg",
      ]);
      map.fire("load");
      await settle();
      map.isSourceLoaded = vi.fn(() => false);
      map.fire("render");
      if (first === "photo") requested[0].decoded();
      else {
        map.isSourceLoaded.mockReturnValue(true);
        map.fire("render");
      }
      await settle();
      await vi.advanceTimersByTimeAsync(3000);
      expect(requested).toHaveLength(1);
      if (first === "photo") {
        map.isSourceLoaded.mockReturnValue(true);
        map.fire("render");
      } else requested[0].decoded();
      await settle();
      expect(requested.map((image) => image.src)).toEqual([
        "/fixture-display-0.jpg",
        "/fixture-display-1.jpg",
      ]);
      expect(requested[1].fetchPriority).toBe("low");
      await vi.advanceTimersByTimeAsync(3000);
      expect(requested).toHaveLength(2);
      requested[1].onload();
      await settle();
      expect(requested[2].src).toBe("/fixture-display-2.jpg");
      requested[2].onload();
      await settle();
      await vi.advanceTimersByTimeAsync(30_000);
      expect(requested).toHaveLength(3);
    },
  );

  it("cancels an old neighbour on navigation and stops on unmount", async () => {
    const requested = photoRequests();
    const map = await mountDay(galleryData());
    map.fire("load");
    await settle();
    map.fire("render");
    requested[0].decoded();
    await settle();
    const neighbour = requested[1];
    button("Next photo").click();
    await settle();
    expect(neighbour.cancelled).toBe(true);
    const current = requested.at(-1);
    expect(current.src).toBe("/fixture-display-1.jpg");
    expect(document.querySelector(".photo img").getAttribute("src")).toBe(
      "/fixture-display-0.jpg",
    );
    current.decoded();
    await settle();
    expect(document.querySelector(".photo img").getAttribute("src")).toBe(
      "/fixture-display-1.jpg",
    );
    const pending = requested.at(-1);
    expect(pending.src).toBe("/fixture-display-2.jpg");
    await unmount(instance);
    instance = null;
    expect(pending.cancelled).toBe(true);
    const count = requested.length;
    pending.onload();
    await settle();
    expect(requested).toHaveLength(count);
  });

  it("waits for the new day route to paint when navigation reuses the map", async () => {
    const requested = photoRequests();
    const data = tripsData();
    await render("day", data);
    const map = maps[0];
    requested[0].decoded();
    await settle();
    const oldNeighbour = requested.at(-1);
    expect(oldNeighbour.fetchPriority).toBe("low");
    map.isSourceLoaded = vi.fn(() => false);

    dataStore.set({ ...data, dayNumber: 2 });
    await settle();
    expect(maps).toHaveLength(1);
    expect(map.remove).not.toHaveBeenCalled();
    expect(oldNeighbour.cancelled).toBe(true);
    const visible = requested.at(-1);
    expect(visible.src).toBe(data.points[3].imgDisplay);
    visible.decoded();
    await settle();
    const beforePaint = requested.length;
    expect(
      requested.filter((image) => image.fetchPriority === "low"),
    ).toHaveLength(1);
    map.fire("render");
    await settle();
    expect(requested).toHaveLength(beforePaint);

    map.isSourceLoaded.mockReturnValue(true);
    map.fire("render");
    await settle();
    expect(requested).toHaveLength(beforePaint + 1);
    expect(requested.at(-1).src).toBe(data.points[4].imgDisplay);
    expect(requested.at(-1).fetchPriority).toBe("low");

    // Scrubbing within the painted day does not wait for another map frame.
    const routeUpdates = map.sources.route.setData.mock.calls.length;
    map.isSourceLoaded.mockReturnValue(false);
    button("Next photo").click();
    await settle();
    const nextVisible = requested.at(-1);
    expect(nextVisible.src).toBe(data.points[4].imgDisplay);
    nextVisible.decoded();
    await settle();
    expect(requested.at(-1).src).toBe(data.points[5].imgDisplay);
    expect(requested.at(-1).fetchPriority).toBe("low");
    expect(map.sources.route.setData).toHaveBeenCalledTimes(routeUpdates);
  });

  it("pauses neighbours for changed route geometry until the replacement source paints", async () => {
    const requested = photoRequests();
    const data = tripsData();
    await render("day", data);
    const map = maps[0];
    requested[0].decoded();
    await settle();
    const oldNeighbour = requested.at(-1);
    map.isSourceLoaded = vi.fn(() => false);
    dataStore.set({
      ...data,
      points: data.points.map((point) => ({ ...point, lat: point.lat + 1 })),
    });
    await settle();
    expect(oldNeighbour.cancelled).toBe(true);
    const beforePaint = requested.length;
    expect(
      requested.filter((image) => image.fetchPriority === "low"),
    ).toHaveLength(1);
    map.fire("render");
    await settle();
    expect(requested).toHaveLength(beforePaint);
    map.isSourceLoaded.mockReturnValue(true);
    map.fire("render");
    await settle();
    expect(requested).toHaveLength(beforePaint + 1);
    expect(requested.at(-1).src).toBe(data.points[1].imgDisplay);
    expect(maps).toHaveLength(1);
  });

  it("waits for a new map after visiting a missing day", async () => {
    const requested = photoRequests();
    const data = tripsData();
    await render("day", data);
    requested[0].decoded();
    await settle();
    expect(requested).toHaveLength(2);
    dataStore.set({ ...data, dayNumber: 9 });
    await settle();
    expect(maps[0].remove).toHaveBeenCalledOnce();
    dataStore.set({ ...data, dayNumber: 2 });
    await settle();
    await vi.waitFor(() => expect(maps).toHaveLength(2));
    const current = requested.at(-1);
    expect(current.src).toBe(data.points[3].imgDisplay);
    current.decoded();
    await settle();
    const beforeMap = requested.length;
    expect(
      requested.filter((image) => image.fetchPriority === "low"),
    ).toHaveLength(1);
    maps[1].fire("load");
    await settle();
    expect(requested).toHaveLength(beforeMap);
    maps[1].fire("render");
    await settle();
    expect(requested).toHaveLength(beforeMap + 1);
    expect(requested.at(-1).src).toBe(data.points[4].imgDisplay);
  });

  it.each(["save-data", "decode-error"])(
    "keeps optional requests off for %s",
    async (condition) => {
      const requested = photoRequests();
      if (condition === "save-data")
        Object.defineProperty(navigator, "connection", {
          configurable: true,
          value: { saveData: true },
        });
      try {
        const map = await mountDay(galleryData());
        map.fire("load");
        await settle();
        map.fire("render");
        if (condition === "save-data") requested[0].decoded();
        else requested[0].failed(new Error("image unavailable"));
        await settle();
        expect(requested).toHaveLength(1);
        expect(button("Next photo").disabled).toBe(false);
      } finally {
        if (condition === "save-data") delete navigator.connection;
      }
    },
  );
});
