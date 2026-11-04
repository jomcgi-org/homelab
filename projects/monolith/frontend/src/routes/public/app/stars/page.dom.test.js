// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/stars/Route.svelte";
import { setPage, page } from "../../../../../test/profile-page.svelte.js";
import { data, history, now } from "../../../../../test/stars/fixtures.js";
import { instances } from "../../../../../test/stars/maplibre.js";

vi.mock("$app/environment", () => ({ browser: true }));
vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
vi.mock("maplibre-gl", () => import("../../../../../test/stars/maplibre.js"));
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
async function render(query = "", props = data) {
  setPage(`https://jomcgi.dev/app/stars${query}`, props);
  instance = mount(Route, { target, props: { data: props } });
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

describe("stars through the real page and public/root layouts", () => {
  it.each(["", "?mode=historical&month=10"])(
    "renders one sheet and working controls for %s",
    async (query) => {
      const map = await render(query);
      expect(target.querySelectorAll("main.td.sheet")).toHaveLength(1);
      expect(target.querySelectorAll(".chrome button.scheme")).toHaveLength(1);
      expect(target.querySelectorAll("h1")).toHaveLength(1);
      expect(target.querySelector(".md-nav")).toBeNull();
      expect(
        target.querySelector('.spine nav[aria-label="Site"]'),
      ).not.toBeNull();
      expect(
        target.querySelector('.trail [aria-current="page"]').textContent,
      ).toBe("stars");
      expect(
        target.querySelector('[role="group"][aria-label="Map mode"]'),
      ).not.toBeNull();
      expect(
        button(query ? "Historical" : "Live").getAttribute("aria-pressed"),
      ).toBe("true");
      expect(map.getSource("sites-cells").setData).toHaveBeenCalled();
      expect(map.canvas.getAttribute("aria-label")).toBe("Dark-sky sites");
    },
  );

  it("preserves URL-backed night/month controls, including unrelated params", async () => {
    const map = await render("?night=2026-10-20&source=shared");
    expect(target.querySelector(".filter-current").textContent).toBe("Tue 20");
    target.querySelector(".filter-toggle").click();
    await settle();
    expect(
      target.querySelector(".filter-toggle").getAttribute("aria-expanded"),
    ).toBe("true");
    await click("Mon 19");
    expect(page.url.searchParams.get("night")).toBe("2026-10-19");
    await click("Historical");
    target.querySelector(".filter-toggle").click();
    await settle();
    await click("Dec");
    expect(page.url.searchParams.get("month")).toBe("12");
    expect(page.url.searchParams.has("night")).toBe(false);
    expect(page.url.searchParams.get("source")).toBe("shared");
    target.querySelector(".filter-toggle").click();
    await settle();
    await click("All year");
    expect(page.url.searchParams.has("month")).toBe(false);
    await click("Live");
    expect(page.url.searchParams.get("night")).toBe("2026-10-19");
    expect(instances).toHaveLength(1);
    expect(map.fitBounds).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith("/app/stars/history-map");
  });

  it.each(["", "?mode=historical&month=10"])(
    "keeps the selected site, sources, camera and URL through repeated schemes in %s",
    async (query) => {
      const map = await render(query);
      map.fire(
        "click",
        { features: [{ properties: { id: 5 } }] },
        "sites-cells",
      );
      await settle();
      const card = target.querySelector(".card");
      expect(card).not.toBeNull();
      expect(card.textContent).toContain(query ? "Clear rate" : "Cloud");
      expect(card.querySelector('a[href*="query=56.386"]')).not.toBeNull();
      if (query) expect(card.querySelector('svg[role="img"]')).not.toBeNull();
      const sources = { ...map.sources };
      const writes = sources["sites-cells"].setData.mock.calls.length;
      const url = page.url.href;
      for (const scheme of ["dark", "light", "dark", "light"]) {
        target.querySelector(".scheme").click();
        await window.happyDOM.waitUntilComplete();
        await settle();
        expect(document.documentElement.dataset.theme).toBe(scheme);
        expect(target.querySelector(".card")).toBe(card);
        expect(map.sources).toEqual(sources);
        expect(map.getSource("sites-cells").setData).toHaveBeenCalledTimes(
          writes,
        );
        expect(page.url.href).toBe(url);
      }
      expect(map.setPaintProperty).toHaveBeenCalledWith(
        "background",
        "background-color",
        "rgb(24, 26, 32)",
      );
      expect(map.setPaintProperty).toHaveBeenCalledWith(
        "background",
        "background-color",
        "rgb(255, 255, 255)",
      );
      expect(instances).toHaveLength(1);
      expect(map.fitBounds).toHaveBeenCalledTimes(1);
    },
  );

  it("responds to system scheme changes and cleans observers, timers and listeners on teardown", async () => {
    const map = await render();
    const paints = map.setPaintProperty.mock.calls.length;
    media.matches = true;
    mediaChange();
    expect(map.setPaintProperty.mock.calls.length).toBeGreaterThan(paints);
    map.canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await settle();
    expect(target.querySelector(".card")).not.toBeNull();
    map.canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await settle();
    expect(target.querySelector(".card")).toBeNull();
    await unmount(instance);
    instance = undefined;
    expect(observerDisconnect).toHaveBeenCalled();
    expect(resizeDisconnect).toHaveBeenCalledOnce();
    expect(media.removeEventListener).toHaveBeenCalled();
    expect(map.remove).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("shows loading, then history error without a retry loop, and retries on mode re-entry", async () => {
    let resolve;
    fetch.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    await render("?mode=historical");
    expect(target.querySelector('[role="status"]').textContent).toContain(
      "Loading history",
    );
    resolve({ ok: false });
    await settle();
    expect(target.querySelector('[role="status"]').textContent).toContain(
      "Historical data is unavailable",
    );
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(2); // existing idle warm-up retries once
    await click("Live");
    await click("Historical");
    expect(target.querySelector('[role="status"]')).toBeNull();
  });

  it("renders live and historical empty states", async () => {
    fetch.mockResolvedValue({ ok: true, json: async () => ({ sites: [] }) });
    await render("", { snapshot: { count: 0, sites: [] } });
    expect(target.querySelector('[role="status"]').textContent).toContain(
      "No dark-sky windows",
    );
    await click("Historical");
    expect(target.querySelector('[role="status"]').textContent).toContain(
      "No clear dark hours across the year",
    );
  });

  it.each(["twilight", "none"])(
    "retains the %s darkness fallback",
    async (darkness) => {
      await render("", { snapshot: { ...data.snapshot, darkness } });
      expect(target.querySelector('[role="status"]').textContent).toContain(
        darkness === "none"
          ? "never gets properly dark"
          : "Showing the darkest twilight",
      );
    },
  );

  it("keeps invalidation polling and removes elapsed nights", async () => {
    await render("?night=2026-10-19");
    const { invalidateAll } = await import("$app/navigation");
    await vi.advanceTimersByTimeAsync(30 * 60_000);
    expect(invalidateAll).toHaveBeenCalled();
    vi.setSystemTime(new Date("2026-10-20T12:00:00Z"));
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    await settle();
    expect(page.url.searchParams.has("night")).toBe(false);
  });
});
