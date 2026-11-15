// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { fromStore, writable } from "svelte/store";
import Route from "../../../../../test/stars/Route.svelte";
import { setPage, page } from "../../../../../test/profile-page.svelte.js";
import { data, history, now } from "../../../../../test/stars/fixtures.js";
import {
  instances,
  initialization,
} from "../../../../../test/stars/maplibre.js";
import { historyView, monthShort } from "$lib/public/stars/heat.js";

vi.mock("$app/environment", () => ({ browser: true }));
vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
vi.mock("maplibre-gl", () => import("../../../../../test/stars/maplibre.js"));
vi.mock("$lib/public/stars/heat.js", async (original) => {
  const helpers = await original();
  return { ...helpers, historyView: vi.fn(helpers.historyView) };
});
// The map boundary owns its worker too. Resolving Vite's worker URL in the
// DOM runner escapes Bazel's test filesystem; the real preview builds it.
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/stars-test-worker.js",
}));
vi.mock("$app/navigation", () => ({
  goto: vi.fn((url) => setPage(url, data)),
  invalidateAll: vi.fn(),
}));
// happy-dom has no canvas rasterizer. Only color resolution is replaced; the
// real observer and paint application run. Chromium checks actual CSS colors.
vi.mock("$lib/public/stars/mapTheme.js", async (original) => ({
  ...(await original()),
  readMapPalette: () =>
    Object.fromEntries(
      [
        "sheet",
        "ink",
        "ink-2",
        "stroke",
        "band",
        "water",
        "low",
        "quarter",
        "mid",
        "upper",
        "high",
      ].map((role) => [
        role,
        document.documentElement.dataset.theme === "dark"
          ? "rgb(24, 26, 32)"
          : "rgb(255, 255, 255)",
      ]),
    ),
}));

let instance;
let target;
let media;
beforeEach(() => {
  initialization.error = null;
  vi.useFakeTimers({
    toFake: [
      "Date",
      "setTimeout",
      "clearTimeout",
      "setInterval",
      "clearInterval",
    ],
  });
  vi.setSystemTime(new Date(now));
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  target = document.createElement("div");
  document.body.append(target);
  instances.length = 0;
  media = {
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  vi.spyOn(window, "matchMedia").mockReturnValue(media);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => history })),
  );
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function settle() {
  for (let i = 0; i < 12; i++) await tick();
}
async function render(query = "", props = data, readData = () => props) {
  setPage(`https://jomcgi.dev/app/stars${query}`, props);
  instance = mount(Route, {
    target,
    props: {
      get data() {
        return readData();
      },
    },
  });
  await vi.waitFor(() =>
    expect(instances[0]?.getSource("sites-cells")).toBeDefined(),
  );
  await settle();
  return instances[0];
}
const button = (text) =>
  [...target.querySelectorAll("button")].find(
    (b) => b.textContent.trim().replace(/\s+/g, " ") === text,
  );
async function click(text) {
  button(text).click();
  await settle();
}

describe("stars immutable history and map updates", () => {
  it("keeps the immutable history plain through every month and site selection", async () => {
    const original = structuredClone(history);
    historyView.mockClear();
    const map = await render();
    map.fire("render");
    await vi.advanceTimersByTimeAsync(1000);
    await settle();
    map.getSource("sites-cells").setData.mockClear();
    map.getSource("sites").setData.mockClear();
    await click("Historical");
    // Regress the costly boundary itself, without a machine-speed threshold:
    // projecting a whole snapshot must not allocate a reactive proxy tree.
    expect(historyView.mock.calls.at(-1)[0]).toBe(history.sites);
    expect(map.getSource("sites-cells").setData).toHaveBeenCalledOnce();
    expect(map.getSource("sites").setData).toHaveBeenCalledOnce();
    const sourceData = (id) => {
      const url = map.getSource(id).setData.mock.calls.at(-1)[0];
      expect(url).toMatch(/^data:application\/json;base64,/);
      return JSON.parse(atob(url.slice(url.indexOf(",") + 1)));
    };
    const cells = () => sourceData("sites-cells").features;
    const expected = (month) => {
      const rows = history.sites.map((site) => ({
        id: site.id,
        clear:
          month === 0
            ? site.clear.reduce((sum, value) => sum + value, 0)
            : site.clear[month - 1],
      }));
      const max = Math.max(1, ...rows.map((row) => row.clear));
      return rows
        .filter((row) => row.clear > 0)
        .sort((a, b) => b.clear - a.clear)
        .map((row) => ({ id: row.id, score: (100 * row.clear) / max }));
    };
    for (const month of [
      0,
      ...Array.from({ length: 12 }, (_, i) => i + 1),
      0,
    ]) {
      if (month || page.url.searchParams.has("month")) {
        target.querySelector(".filter-toggle").click();
        await settle();
        await click(month ? monthShort(month) : "All year");
      }
      expect(cells().map((feature) => feature.properties)).toEqual(
        expected(month),
      );
      const points = sourceData("sites").features;
      expect(points.map((feature) => feature.properties)).toEqual(
        expected(month),
      );
      for (const point of points) {
        const site = history.sites.find((item) => item.id === point.id);
        expect(point.geometry).toEqual({
          type: "Point",
          coordinates: [site.lon, site.lat],
        });
      }
      expect(historyView.mock.calls.at(-1)[0]).toBe(history.sites);
    }
    map.fire("click", { features: [{ properties: { id: 5 } }] }, "sites-cells");
    await settle();
    const card = target.querySelector(".card");
    expect(card.textContent).toContain("Clear rate");
    expect(card.querySelectorAll(".chart-tick")).toHaveLength(12);
    await click("Live");
    expect(target.querySelector(".card")).toBeNull();
    await click("Historical");
    expect(cells().map((feature) => feature.properties)).toEqual(expected(0));
    expect(history).toEqual(original);
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("publishes a replaced forecast snapshot and keeps every refreshed site selectable", async () => {
    const routeData = fromStore(writable(data));
    const map = await render("", data, () => routeData.current);
    const points = map.getSource("sites").setData;
    const cells = map.getSource("sites-cells").setData;
    points.mockClear();
    cells.mockClear();
    const refreshed = data.snapshot.sites.map((site) => ({
      ...site,
      id: site.id + 100,
      name: `Refreshed ${site.name}`,
      lat: site.lat + 0.01,
    }));
    routeData.current = {
      ...data,
      snapshot: { ...data.snapshot, sites: refreshed },
    };
    await settle();
    expect(points).toHaveBeenCalledOnce();
    expect(cells).toHaveBeenCalledOnce();
    expect(
      cells.mock.calls[0][0].features.map((feature) => feature.id),
    ).toEqual(refreshed.map((site) => site.id));
    for (const site of refreshed) {
      map.fire(
        "click",
        { features: [{ properties: { id: site.id } }] },
        "sites-cells",
      );
      await settle();
      expect(
        target
          .querySelector('.card a[href*="maps/search"]')
          .getAttribute("href"),
      ).toContain(`query=${site.lat},${site.lon}`);
    }
    expect(map.fitBounds).toHaveBeenCalledOnce();
  });
});
