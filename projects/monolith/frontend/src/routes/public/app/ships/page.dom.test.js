// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { fromStore, writable } from "svelte/store";
import Route from "../../../../../test/ProfileRoute.svelte";
import { setPage, page } from "../../../../../test/profile-page.svelte.js";
import { maps, initialization } from "../../../../../test/ships-map-double.js";
import {
  snapshot,
  heat,
  vesselTrack,
} from "../../../../../test/ships-fixtures.js";
import { goto, invalidateAll } from "$app/navigation";
import mapSource from "$lib/public/components/ships/ShipsMap.svelte?raw";
import snapshotSource from "$lib/public/components/MapSnapshot.svelte?raw";
import { deadReckon } from "$lib/public/ships/deadReckoning.js";

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
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/ships-test-worker.js",
}));
vi.mock("$app/navigation", () => ({ goto: vi.fn(), invalidateAll: vi.fn() }));

let instance, target, media, fetchMock, idle, styles, routeData;
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
  initialization.error = null;
  vi.useFakeTimers();
  maps.length = 0;
  idle = undefined;
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
  styles?.remove();
  styles = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
});

async function render(query = "", data = snapshot, load = true) {
  setPage(`https://jomcgi.dev/app/ships${query}`);
  routeData = fromStore(writable({ snapshot: data }));
  instance = mount(Route, {
    target,
    props: {
      surface: "ships",
      get data() {
        return routeData.current;
      },
    },
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
  const fixedAt = Date.parse("2026-11-14T12:00:00Z");
  const frameAt = (time) => {
    vi.setSystemTime(time);
    requestAnimationFrame.mock.calls.at(-1)[0]();
  };

  it("keeps capped and stationary positions without resubmitting idle frames", async () => {
    vi.setSystemTime(fixedAt);
    const vessels = [
      snapshot.vessels[0],
      {
        ...snapshot.vessels[1],
        speed: 0,
        recorded_at: new Date(fixedAt).toISOString(),
      },
      {
        ...snapshot.vessels[2],
        speed: null,
        recorded_at: new Date(fixedAt + 60000).toISOString(),
      },
    ];
    const map = await render("", { vessels });
    const source = map.getSource("vessels");
    const before = structuredClone(source.data);
    source.setData.mockClear();
    frameAt(fixedAt + 250);
    frameAt(fixedAt + 500);
    expect(source.setData).not.toHaveBeenCalled();
    expect(source.updateData).not.toHaveBeenCalled();
    expect(source.data).toEqual(before);
    expect(source.data.features).toHaveLength(3);

    map.emit("mousemove", { features: [{ id: "900000002" }] }, "vessels");
    map.emit("click", { features: [{ id: "900000001" }] }, "vessels");
    await settle();
    expect(source.setData).toHaveBeenCalledTimes(2);
    expect(source.data.features[0].properties.sel).toBe(true);
    expect(source.data.features[1].properties.hov).toBe(true);
    expect(source.data.features.map((feature) => feature.geometry)).toEqual(
      before.features.map((feature) => feature.geometry),
    );
    map.emit("mouseleave", {}, "vessels");
    target.querySelector('[aria-label="Close vessel panel"]').click();
    await settle();
    expect(source.data).toEqual(before);
  });

  it("renders every moving tick and the final capped position before becoming idle", async () => {
    vi.setSystemTime(fixedAt);
    const vessels = snapshot.vessels.slice(0, 2).map((vessel, index) => ({
      ...vessel,
      recorded_at: new Date(fixedAt - 299750 + index * 250).toISOString(),
    }));
    const map = await render("", { vessels });
    const source = map.getSource("vessels");
    source.setData.mockClear();
    for (const elapsed of [250, 500]) {
      frameAt(fixedAt + elapsed);
      expect(source.updateData).toHaveBeenCalledTimes(elapsed / 250);
      expect(
        source.data.features.map((feature) => feature.geometry.coordinates),
      ).toEqual(
        vessels.map((vessel) => {
          const position = deadReckon(
            vessel,
            Math.min(
              (fixedAt + elapsed - Date.parse(vessel.recorded_at)) / 1000,
              300,
            ),
          );
          return [position.lon, position.lat];
        }),
      );
    }
    const capped = structuredClone(source.data);
    frameAt(fixedAt + 750);
    frameAt(fixedAt + 1000);
    expect(source.setData).not.toHaveBeenCalled();
    expect(
      source.updateData.mock.calls.map(([diff]) =>
        diff.update.map((entry) => entry.id),
      ),
    ).toEqual([["900000001", "900000002"], ["900000002"]]);
    expect(source.data).toEqual(capped);
  });

  it("patches moving geometry while keeping the full fleet and interaction properties", async () => {
    vi.setSystemTime(fixedAt);
    const vessels = snapshot.vessels.map((vessel, index) => ({
      ...vessel,
      recorded_at:
        index < 2 ? new Date(fixedAt).toISOString() : vessel.recorded_at,
    }));
    const map = await render("?mmsi=900000001", { vessels });
    const source = map.getSource("vessels");
    map.emit("mousemove", { features: [{ id: "900000002" }] }, "vessels");
    const before = structuredClone(source.data);
    source.setData.mockClear();
    frameAt(fixedAt + 250);
    expect(source.setData).not.toHaveBeenCalled();
    expect(source.updateData).toHaveBeenCalledTimes(1);
    expect(
      source.updateData.mock.calls[0][0].update.map((entry) => entry.id),
    ).toEqual(["900000001", "900000002"]);
    expect(source.data.features).toHaveLength(48);
    expect(source.data.features.slice(2)).toEqual(before.features.slice(2));
    expect(source.data.features.map((feature) => feature.properties)).toEqual(
      before.features.map((feature) => feature.properties),
    );
    expect(
      source.data.features
        .slice(0, 2)
        .map((feature) => feature.geometry.coordinates),
    ).toEqual(
      vessels.slice(0, 2).map((vessel) => {
        const position = deadReckon(vessel, 0.25);
        return [position.lon, position.lat];
      }),
    );
  });

  it("resumes animation on a refreshed fix and drops removed vessels", async () => {
    vi.setSystemTime(fixedAt);
    const map = await render("", { vessels: snapshot.vessels.slice(0, 2) });
    const source = map.getSource("vessels");
    source.setData.mockClear();
    frameAt(fixedAt + 250);
    expect(source.setData).not.toHaveBeenCalled();
    const vessel = {
      ...snapshot.vessels[0],
      lat: 51,
      lon: -2,
      recorded_at: new Date(fixedAt + 250).toISOString(),
    };
    routeData.current = { snapshot: { vessels: [vessel] } };
    await settle();
    expect(source.setData).toHaveBeenCalledTimes(1);
    expect(source.data.features.map((feature) => feature.id)).toEqual([
      "900000001",
    ]);
    frameAt(fixedAt + 500);
    expect(source.setData).toHaveBeenCalledTimes(1);
    expect(source.updateData).toHaveBeenCalledTimes(1);
    const position = deadReckon(vessel, 0.25);
    expect(source.data.features[0].geometry.coordinates).toEqual([
      position.lon,
      position.lat,
    ]);
  });

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
    map.emit("render");
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

  it.each(["HTTP error", "network error"])(
    "keeps controls above the snapshot after a shared heat view fails with %s",
    async (failure) => {
      if (failure === "HTTP error")
        fetchMock.mockResolvedValueOnce({ ok: false });
      else fetchMock.mockRejectedValueOnce(new Error("offline"));
      const map = await render("?mode=heat");
      map.emit("render");
      await settle();
      const preview = target.querySelector(".map-snapshot");
      expect(preview.textContent).toContain("Fixture vessel 1");
      expect(preview.querySelectorAll("li")).toHaveLength(6);
      expect(button("Heat").disabled).toBe(false);
      expect(button("Heat").hasAttribute("aria-label")).toBe(false);

      // Happy DOM cannot hit-test a pointer. Load the actual component styles
      // and verify the positioned controls stack above the retained snapshot;
      // a programmatic click alone would miss the original blocked-control bug.
      styles = document.createElement("style");
      styles.textContent = [mapSource, snapshotSource]
        .map((source) => source.match(/<style>([\s\S]*?)<\/style>/)[1])
        .join("\n");
      document.head.append(styles);
      const snapshotZ = Number(getComputedStyle(preview).zIndex);
      for (const selector of [".mode-toggle", ".legend"]) {
        const style = getComputedStyle(target.querySelector(selector));
        expect(style.position).toBe("absolute");
        expect(Number(style.zIndex)).toBeGreaterThan(snapshotZ);
        expect(style.pointerEvents).not.toBe("none");
      }

      button("Vessels").click();
      await settle();
      expect(map.layers.get("vessels").layout.visibility).toBe("visible");
      expect(map.layers.get("heat-fill").layout.visibility).toBe("none");
      expect(page.url.searchParams.has("mode")).toBe(false);
      button("Passenger").click();
      await settle();
      expect(preview.textContent).toContain("40 vessels");
      expect(preview.querySelector("li").textContent).toContain(
        "Fixture vessel 2",
      );
      map.emit("render");
      await settle();
      expect(target.querySelector(".map-snapshot")).toBeNull();
      button("Heat").click();
      await settle();
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(map.getSource("heat-cells").data.features).toHaveLength(180);
    },
  );

  it("deduplicates pending heat fetches and aborts them with the idle callback, RAF and poller on teardown", async () => {
    const pending = deferred();
    fetchMock.mockReturnValue(pending.promise);
    const map = await render();
    map.emit("render");
    idle();
    button("Vessels").click();
    button("Heat").click();
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(button("Heat").disabled).toBe(false);
    expect(button("Vessels").disabled).toBe(false);
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
    const map = await render();
    map.emit("render");
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

  it("keeps the snapshot until a frame contains its data", async () => {
    const map = await render();
    const preview = target.querySelector(".map-snapshot");
    expect(preview.textContent).toContain("Fixture vessel 1");
    expect(preview.querySelectorAll("li")).toHaveLength(6);
    map.sourceLoaded = false;
    map.emit("render");
    await settle();
    expect(target.querySelector(".map-snapshot")).toBe(preview);
    map.sourceLoaded = true;
    map.emit("render");
    await settle();
    expect(target.querySelector(".map-snapshot")).toBeNull();
  });

  it.each(["", "?mode=heat"])(
    "disables unavailable Heat and retains vessel filters after WebGL failure for %s",
    async (query) => {
      initialization.error = new Error("WebGL unavailable");
      const log = vi.spyOn(console, "error").mockImplementation(() => {});
      setPage(`https://jomcgi.dev/app/ships${query}`, { snapshot });
      instance = mount(Route, {
        target,
        props: { surface: "ships", data: { snapshot } },
      });
      await vi.waitFor(() => expect(log).toHaveBeenCalled());
      await settle();
      expect(target.querySelector(".map-snapshot").textContent).toContain(
        "Fixture vessel 1",
      );
      expect(target.querySelector(".snapshot-note").textContent).toContain(
        "map is unavailable",
      );
      expect(button("Heat").disabled).toBe(true);
      expect(button("Heat").getAttribute("aria-label")).toBe(
        "Heat (interactive map unavailable)",
      );
      expect(button("Heat").title).toContain("requires the interactive map");
      button("Heat").click();
      await settle();
      expect(button("Heat").getAttribute("aria-pressed")).toBe(String(!!query));

      expect(button("Vessels").disabled).toBe(false);
      button("Vessels").click();
      await settle();
      expect(button("Vessels").getAttribute("aria-pressed")).toBe("true");
      expect(page.url.searchParams.has("mode")).toBe(false);
      button("Passenger").click();
      await settle();
      const preview = target.querySelector(".map-snapshot");
      expect(preview.textContent).toContain("40 vessels");
      expect(preview.querySelector("li").textContent).toContain(
        "Fixture vessel 2",
      );
      expect(maps).toHaveLength(0);
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("defers optional heat warming until vessel data paints", async () => {
    const map = await render();
    expect(requestIdleCallback).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    map.emit("render");
    expect(requestIdleCallback).toHaveBeenCalledOnce();
    idle();
    await settle();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("/app/ships/heat");
  });
});
