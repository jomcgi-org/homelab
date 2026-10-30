// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/ProfileRoute.svelte";
import { setPage, page } from "../../../../../test/profile-page.svelte.js";
import { maps } from "../../../../../test/ships-map-double.js";
import {
  snapshot,
  heat,
  vesselTrack,
} from "../../../../../test/ships-fixtures.js";
import { goto, invalidateAll } from "$app/navigation";

vi.mock("$app/environment", () => ({
  browser: true,
  version: "test",
  dev: false,
}));
vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
vi.mock("maplibre-gl", () => import("../../../../../test/ships-map-double.js"));
vi.mock("$app/navigation", () => ({ goto: vi.fn(), invalidateAll: vi.fn() }));

let instance, target, media, fetchMock, idle;
const settle = async () => {
  await vi.dynamicImportSettled();
  await tick();
  await vi.advanceTimersByTimeAsync(0);
  await tick();
};
const button = (text) =>
  [...target.querySelectorAll("button")].find(
    (b) => b.textContent.trim() === text,
  );
const response = (body) => ({ ok: true, json: async () => body });
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

beforeEach(() => {
  vi.useFakeTimers();
  maps.length = 0;
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  document.documentElement.dataset.theme = "light";
  localStorage.clear();
  media = {
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  vi.stubGlobal("matchMedia", () => media);
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn(() => 42),
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal(
    "requestIdleCallback",
    vi.fn((fn) => {
      idle = fn;
      return 43;
    }),
  );
  vi.stubGlobal("cancelIdleCallback", vi.fn());
  vi.stubGlobal("Path2D", class {});
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => {
    const ctx = {
      fillStyle: "rgb(1, 2, 3)",
      clearRect() {},
      fillRect() {},
      scale() {},
      fill() {},
      stroke() {},
      getImageData: () => ({
        data: Uint8ClampedArray.from([
          ...ctx.fillStyle.match(/\d+/g).map(Number).slice(0, 3),
          255,
        ]),
      }),
    };
    return ctx;
  });
  const computed = globalThis.getComputedStyle;
  vi.stubGlobal("getComputedStyle", (element) => {
    const styles = computed(element);
    if (!element.classList.contains("map")) return styles;
    // Happy DOM does not evaluate bundled CSS or color-mix. Assert the real
    // palette resolver reads the nested map surface, then supply resolved roles.
    expect(element.closest(".td.ships-sheet")).not.toBeNull();
    return {
      getPropertyValue: (token) => {
        const shade =
          document.documentElement.dataset.theme === "dark" ? 180 : 30;
        const channel =
          [...token].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 70;
        return `rgb(${shade}, ${channel}, 90)`;
      },
    };
  });
  goto.mockReset();
  goto.mockImplementation(async (url) => setPage(url.href, page.data));
  invalidateAll.mockReset();
  fetchMock = vi.fn(async (url) =>
    response(url.endsWith("heat") ? heat : vesselTrack(url.split("/").pop())),
  );
  vi.stubGlobal("fetch", fetchMock);
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  target.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
});

async function render(query = "", data = snapshot, load = true) {
  setPage(`https://jomcgi.dev/app/ships${query}`);
  instance = mount(Route, {
    target,
    props: { surface: "ships", data: { snapshot: data } },
  });
  await settle();
  expect(maps).toHaveLength(1);
  if (load) {
    maps[0].emit("load");
    await settle();
  }
  return maps[0];
}

describe("ships production browser route", () => {
  it("mounts the real app/map under one sheet and chrome, with accessible controls", async () => {
    const map = await render();
    expect(target.querySelectorAll("main.td.sheet")).toHaveLength(1);
    expect(target.querySelectorAll(".chrome, button.scheme")).toHaveLength(2);
    expect(target.querySelector(".md-nav")).toBeNull();
    expect(target.querySelector("h1").textContent).toBe(
      "Live ships, AIS vessel tracker",
    );
    expect(target.querySelector('.trail a[href="/"]')).not.toBeNull();
    expect(
      target.querySelector('[role="group"][aria-label="Map mode"]'),
    ).not.toBeNull();
    expect(
      target.querySelectorAll(".legend-item[aria-pressed=true]"),
    ).toHaveLength(6);
    expect(map.getSource("vessels").data.features).toHaveLength(48);
    expect(map.addSource).toHaveBeenCalledTimes(3);
    expect(map.addLayer).toHaveBeenCalledTimes(3);
    expect(map.addImage).toHaveBeenCalledTimes(6);
    expect(map.options.style).toBe(
      "https://tiles.openfreemap.org/styles/liberty",
    );
    expect(map.options.attributionControl).toEqual({ compact: true });
  });

  it("repaints persisted schemes without losing camera, selection, filter, sources or track", async () => {
    const map = await render("?types=cargo,tanker&mmsi=900000001");
    const source = map.getSource("vessels"),
      track = map.getSource("ship-track").data;
    const filter = map.layers.get("vessels").filter,
      camera = map.camera;
    const ink = map.layers.get("ship-track-line").paint["line-color"];
    const listeners = [...map.listeners];
    target.querySelector("button.scheme").click();
    await settle();
    expect(localStorage.getItem("td-theme")).toBe("dark");
    expect(map.layers.get("ship-track-line").paint["line-color"]).not.toEqual(
      ink,
    );
    expect(map.getSource("vessels")).toBe(source);
    expect(map.getSource("ship-track").data).toBe(track);
    expect(map.camera).toBe(camera);
    expect(map.layers.get("vessels").filter).toEqual(filter);
    expect(map.getSource("vessels").data.features[0].properties.sel).toBe(true);
    expect(target.querySelector(".panel-name").textContent.trim()).toBe(
      "Fixture vessel 1",
    );
    expect(map.addSource).toHaveBeenCalledTimes(3);
    expect(map.addLayer).toHaveBeenCalledTimes(3);
    expect(map.addImage).toHaveBeenCalledTimes(6);
    expect(map.updateImage).toHaveBeenCalledTimes(6);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // System-scheme notifications use the same in-place path.
    media.addEventListener.mock.calls.find(
      ([, callback]) => callback.name === "refreshPalette",
    )[1]();
    expect(map.updateImage).toHaveBeenCalledTimes(12);
    expect(map.fitBounds).not.toHaveBeenCalled();
    expect(map.listeners).toEqual(listeners);
  });

  it("restores heat mode, toggles types, preserves loaded quantile data across schemes and modes", async () => {
    const map = await render(
      "?mode=heat&types=cargo&mmsi=900000001&other=kept",
    );
    expect(target.querySelector(".panel")).toBeNull();
    expect(target.querySelector(".heat-scale").textContent).toContain("250+");
    expect(map.layers.get("vessels").layout.visibility).toBe("none");
    const cells = map.getSource("heat-cells").data;
    const ramp = map.layers.get("heat-fill").paint["fill-color"];
    target.querySelector("button.scheme").click();
    await settle();
    expect(map.getSource("heat-cells").data).toBe(cells);
    expect(map.layers.get("heat-fill").paint["fill-color"]).not.toEqual(ramp);
    button("Vessels").click();
    await settle();
    button("Passenger").click();
    await settle();
    expect(map.layers.get("vessels").filter[2][1]).toEqual([
      "ship-passenger",
      "ship-cargo",
    ]);
    expect(page.url.searchParams.get("types")).toBe("passenger,cargo");
    expect(page.url.searchParams.get("other")).toBe("kept");
    button("Heat").click();
    idle();
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(map.getSource("heat-cells").data).toBe(cells);
  });

  it("keeps fallback legends on an empty or failed grid and can retry", async () => {
    fetchMock
      .mockResolvedValueOnce({ ok: false })
      .mockResolvedValueOnce(response({ ...heat, cells: [], stops: [] }));
    const map = await render("?mode=heat", { vessels: [] });
    expect(map.options.center).toEqual([0, 30]);
    expect(map.getSource("vessels").data.features).toEqual([]);
    expect(target.querySelector(".heat-scale").textContent).toContain("20+");
    button("Vessels").click();
    button("Heat").click();
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(map.getSource("heat-cells").data.features).toEqual([]);
    expect(target.querySelector(".heat-scale").textContent).toContain("1-2");
  });

  it("deduplicates pending heat fetches and aborts them with the idle callback, RAF and poller on teardown", async () => {
    const pending = deferred();
    fetchMock.mockReturnValue(pending.promise);
    const map = await render("?mode=heat");
    idle();
    button("Vessels").click();
    button("Heat").click();
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(120000);
    expect(invalidateAll).toHaveBeenCalledTimes(1);
    const signal = fetchMock.mock.calls[0][1].signal;
    await unmount(instance);
    instance = null;
    expect(signal.aborted).toBe(true);
    pending.resolve(response(heat));
    await settle();
    await vi.advanceTimersByTimeAsync(120000);
    expect(invalidateAll).toHaveBeenCalledTimes(1);
    expect(cancelAnimationFrame).toHaveBeenCalledWith(42);
    expect(cancelIdleCallback).toHaveBeenCalledWith(43);
    expect(map.remove).toHaveBeenCalledTimes(1);
    const writes = map.updateImage.mock.calls.length;
    document.documentElement.dataset.theme = "dark";
    await settle();
    expect(map.updateImage).toHaveBeenCalledTimes(writes);
    expect(media.removeEventListener).toHaveBeenCalled();
  });

  it("rejects late tracks after another selection, deselection or heat mode", async () => {
    const first = deferred();
    fetchMock.mockReturnValueOnce(first.promise);
    const map = await render("?mmsi=900000001");
    expect(target.querySelector(".panel-meta").textContent).toBe(
      "Loading track…",
    );
    map.emit("click", { features: [{ id: "900000002" }] }, "vessels");
    await settle();
    first.resolve(response({ count: 999, track: [] }));
    await settle();
    expect(target.querySelector(".panel-meta").textContent).toBe(
      "8 track points",
    );
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    target.querySelector('[aria-label="Close vessel panel"]').click();
    await settle();
    expect(map.getSource("ship-track").data.features).toEqual([]);
    expect(page.url.searchParams.has("mmsi")).toBe(false);
    const late = deferred();
    fetchMock.mockReturnValueOnce(late.promise);
    map.emit("click", { features: [{ id: "900000003" }] }, "vessels");
    button("Heat").click();
    await settle();
    late.resolve(response({ count: 99, track: [] }));
    await settle();
    expect(target.querySelector(".panel")).toBeNull();
    expect(map.getSource("ship-track").data.features).toEqual([]);
  });

  it("keeps snapshot details on track errors and ignores duplicate load events", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const map = await render("?mmsi=900000001");
    expect(target.querySelector(".panel-name")).not.toBeNull();
    expect(target.querySelector(".panel-meta")).toBeNull();
    map.emit("load");
    expect(map.addSource).toHaveBeenCalledTimes(3);
    expect(requestAnimationFrame).toHaveBeenCalledTimes(1);
  });

  it("removes a map that unmounts before load without scheduling warm work", async () => {
    const map = await render("", snapshot, false);
    await unmount(instance);
    instance = null;
    map.emit("load");
    await settle();
    expect(map.addSource).not.toHaveBeenCalled();
    expect(requestIdleCallback).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("cancels the timeout warmup fallback before it can fetch", async () => {
    vi.stubGlobal("requestIdleCallback", undefined);
    await render();
    await unmount(instance);
    instance = null;
    await vi.advanceTimersByTimeAsync(1500);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not construct a map when unmounted during the lazy import", async () => {
    setPage("https://jomcgi.dev/app/ships");
    instance = mount(Route, {
      target,
      props: { surface: "ships", data: { snapshot } },
    });
    await tick();
    await unmount(instance);
    instance = null;
    await settle();
    expect(maps).toHaveLength(0);
  });
});
