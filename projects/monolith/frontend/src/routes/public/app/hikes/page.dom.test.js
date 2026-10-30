// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/hikes/Route.svelte";
import { setPage, page } from "../../../../../test/profile-page.svelte.js";
import { data, detail, now } from "../../../../../test/hikes/fixtures.js";
import { instances } from "../../../../../test/stars/maplibre.js";

vi.mock("$app/environment", () => ({ browser: true }));
vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
// The MapLibre boundary mock is shared with stars: it records sources, layers,
// paint writes and the camera, and fires `load` once.
vi.mock("maplibre-gl", () => import("../../../../../test/stars/maplibre.js"));
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/hikes-test-worker.js",
}));
vi.mock("$app/navigation", () => ({
  goto: vi.fn((url) => setPage(url, data)),
  invalidateAll: vi.fn(),
}));
// happy-dom has no canvas rasterizer. Only colour resolution is replaced; the
// real observer and paint application run.
vi.mock("$lib/public/hikes/mapTheme.js", async (original) => ({
  ...(await original()),
  readMapPalette: () =>
    Object.fromEntries(
      ["sheet", "ink", "ink-2", "stroke", "band", "water"].map((role) => [
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
let mediaChange;
let resizeDisconnect;
let observerDisconnect;
beforeEach(() => {
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
    addEventListener: vi.fn((_, fn) => {
      mediaChange = fn;
    }),
    removeEventListener: vi.fn(),
  };
  vi.spyOn(window, "matchMedia").mockReturnValue(media);
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
  observerDisconnect = vi.spyOn(MutationObserver.prototype, "disconnect");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => detail })),
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
async function render(query = "", props = data) {
  setPage(`https://jomcgi.dev/app/hikes${query}`, props);
  instance = mount(Route, { target, props: { data: props } });
  await vi.waitFor(() =>
    expect(instances[0]?.getSource("walks")).toBeDefined(),
  );
  await settle();
  return instances[0];
}
const button = (text) =>
  [...target.querySelectorAll("button")].find(
    (b) => b.textContent.trim().replace(/\s+/g, " ") === text,
  );
const plotted = (map) =>
  map.getSource("walks").setData.mock.lastCall[0].features.length;

describe("hikes through the real page and public/root layouts", () => {
  it("renders one sheet with the controls in the spine and the map as the panel", async () => {
    const map = await render();
    expect(target.querySelectorAll("main.td.sheet")).toHaveLength(1);
    expect(target.querySelectorAll(".chrome button.scheme")).toHaveLength(1);
    expect(target.querySelectorAll("h1")).toHaveLength(1);
    expect(target.querySelector(".md-nav")).toBeNull();
    expect(
      target.querySelector('.spine nav[aria-label="Site"]'),
    ).not.toBeNull();
    expect(
      target.querySelector('.trail [aria-current="page"]').textContent,
    ).toBe("hikes");
    expect(
      target.querySelector(".stats").textContent.replace(/\s+/g, " "),
    ).toContain("9 walks · 8 viable today · 8 shown");
    expect(target.querySelector(".content.edition .map")).not.toBeNull();
    expect(target.querySelector(".legend").textContent).toContain("Effort");
    expect(map.canvas.getAttribute("aria-label")).toBe("Hill walks");
    expect(button("Any day").getAttribute("aria-pressed")).toBe("true");
    // Only days the corpus covers get a chip.
    expect(
      [...target.querySelectorAll(".day-chip")].map((b) => b.textContent),
    ).toEqual(["Any day", "Mon 19", "Tue 20", "Wed 21"]);
    // The walk with no viable day is not plotted in "any day" mode.
    expect(plotted(map)).toBe(8);
    expect(map.fitBounds).toHaveBeenCalledTimes(1);
  });

  it("mirrors the day, region and limits to the URL and back", async () => {
    const map = await render("?near=cairngorms&ascent=500&source=shared");
    expect(target.querySelector(".near-field select").value).toBe("cairngorms");
    expect(target.querySelector(".filter-current").textContent).toBe("set");
    expect(plotted(map)).toBe(1);
    button("Tue 20").click();
    await settle();
    expect(page.url.searchParams.get("day")).toBe("2026-10-20");
    expect(page.url.searchParams.get("source")).toBe("shared");
    expect(button("Tue 20").getAttribute("aria-pressed")).toBe("true");
    target.querySelector(".filter-toggle").click();
    await settle();
    expect(
      target.querySelector(".filter-toggle").getAttribute("aria-expanded"),
    ).toBe("true");
    expect(target.querySelectorAll(".num-grid input")).toHaveLength(5);
    button("Reset").click();
    await settle();
    expect(page.url.searchParams.has("ascent")).toBe(false);
    expect(page.url.searchParams.has("near")).toBe(false);
    expect(page.url.searchParams.has("day")).toBe(false);
    expect(target.querySelector(".filter-current").textContent).toBe("none");
    expect(plotted(map)).toBe(8);
    expect(instances).toHaveLength(1);
  });

  it("opens a walk card from the map, keeps it through scheme changes, and closes from the keyboard", async () => {
    const map = await render();
    map.fire(
      "click",
      { features: [{ properties: { uuid: "walk-1" } }] },
      "walks",
    );
    await settle();
    const card = target.querySelector(".card");
    expect(card).not.toBeNull();
    expect(card.querySelector(".card-title a").getAttribute("href")).toBe(
      "https://example.invalid/walks/1",
    );
    expect(fetch).toHaveBeenCalledWith("/app/hikes/walk/walk-1");
    expect(page.url.searchParams.get("walk")).toBe("walk-1");
    await vi.waitFor(() =>
      expect(card.textContent).toContain("Good days ahead"),
    );
    expect(card.querySelectorAll(".card-windows tbody tr")).toHaveLength(5);
    const sources = { ...map.sources };
    const writes = sources.walks.setData.mock.calls.length;
    for (const scheme of ["dark", "light"]) {
      target.querySelector(".scheme").click();
      await window.happyDOM.waitUntilComplete();
      await settle();
      expect(document.documentElement.dataset.theme).toBe(scheme);
      expect(target.querySelector(".card")).toBe(card);
      expect(map.sources).toEqual(sources);
      expect(map.getSource("walks").setData).toHaveBeenCalledTimes(writes);
    }
    expect(map.setPaintProperty).toHaveBeenCalledWith(
      "background",
      "background-color",
      "rgb(24, 26, 32)",
    );
    expect(map.setPaintProperty).toHaveBeenCalledWith(
      "walks",
      "circle-stroke-color",
      "rgb(255, 255, 255)",
    );
    map.canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await settle();
    expect(target.querySelector(".card")).toBeNull();
    expect(page.url.searchParams.has("walk")).toBe(false);
    expect(map.fitBounds).toHaveBeenCalledTimes(1);
  });

  it("responds to system scheme changes and cleans observers, timers and listeners on teardown", async () => {
    const map = await render();
    const paints = map.setPaintProperty.mock.calls.length;
    media.matches = true;
    mediaChange();
    expect(map.setPaintProperty.mock.calls.length).toBeGreaterThan(paints);
    await unmount(instance);
    instance = undefined;
    expect(observerDisconnect).toHaveBeenCalled();
    expect(resizeDisconnect).toHaveBeenCalledOnce();
    expect(media.removeEventListener).toHaveBeenCalled();
    expect(map.remove).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });
});
