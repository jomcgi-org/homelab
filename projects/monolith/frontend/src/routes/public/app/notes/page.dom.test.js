// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { readFileSync } from "node:fs";
import Page from "./+page.svelte";
import PrivatePage from "../../../private/notes/+page.svelte";
import GraphView from "$lib/public/notes/GraphView.svelte";
import { setCachedGraph } from "$lib/public/notes/graph-cache.js";
import { graphFixture, noteFixture } from "$lib/public/notes/graph-fixture.js";
import { resolveGraphPalette } from "$lib/components/notes/canvas-palette.js";
import { page } from "$app/state";

const readStyle = (path) =>
  readFileSync(new URL(path, import.meta.url), "utf8");
const tokens = readStyle("../../../../lib/public/styles/technical-drawing.css");
const sheet = readStyle("../../../../lib/public/notes/sheet.css");

vi.mock("$app/environment", () => ({ browser: true }));

let instance;
let target;
let styles;
let paints;
let ctx;
let fetchMock;

const freshHeaders = () =>
  new Headers({ "cache-control": "public, max-age=300" });

beforeEach(() => {
  // happy-dom 20.8.9 holds its MutationObserver listener callback only in a
  // WeakRef. Pin that callback during the test so GC cannot silently drop it.
  // The real observer, CSS resolver and component subscription still run.
  vi.stubGlobal(
    "WeakRef",
    class extends WeakRef {
      constructor(value) {
        super(value);
        this.keepAlive = value;
      }
    },
  );
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  document.documentElement.dataset.theme = "light";
  page.url = new URL("https://public.example.test/app/notes");
  setCachedGraph(null);
  styles = document.createElement("style");
  styles.textContent = tokens + sheet;
  document.head.append(styles);
  target = document.createElement("div");
  document.body.append(target);
  paints = [];
  ctx = {
    measureText: (text) => ({ width: text.length * 6 }),
    clearRect: vi.fn(),
  };
  for (const method of [
    "arc",
    "beginPath",
    "fillRect",
    "fillText",
    "lineTo",
    "moveTo",
    "restore",
    "save",
    "scale",
    "setTransform",
    "strokeRect",
    "translate",
    "fill",
    "stroke",
  ]) {
    ctx[method] = vi.fn((...args) =>
      paints.push({
        method,
        args,
        fill: ctx.fillStyle,
        stroke: ctx.strokeStyle,
        alpha: ctx.globalAlpha,
      }),
    );
  }
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(900);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(640);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  // The graph cache now reads the response's cache lease (max-age minus
  // age), so a fixture response carries one the way the proxy does.
  fetchMock = vi.fn(async (url) => ({
    ok: true,
    headers: freshHeaders(),
    json: async () => (url === "/app/notes/graph" ? graphFixture : noteFixture),
  }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  styles.remove();
  setCachedGraph(null);
  delete document.documentElement.dataset.theme;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function render(component = Page, props = {}) {
  instance = mount(component, { target, props });
  await tick();
  return target;
}

async function loaded() {
  await vi.waitFor(() => expect(target.querySelector("canvas")).not.toBeNull());
  await tick();
  await vi.waitFor(() =>
    expect(target.querySelector("canvas").__transition).toBeUndefined(),
  );
}

describe("public notes sheet DOM", () => {
  const overviewData = (leaseMs = 60_000) => ({
    overview: {
      nodeCount: 7,
      edgeCount: 6,
      titles: [{ id: "sheet-note", title: "First-paint public title" }],
      expiresAt: Date.now() + leaseMs,
      leaseMs,
      leaseToken: "test-response",
    },
    overviewDeadline: performance.now() + leaseMs,
    overviewClock: { monotonicAt: performance.now(), wallAt: Date.now() },
  });

  it("keeps real titles visible until the canvas has painted", async () => {
    let release;
    fetchMock.mockImplementationOnce(
      () => new Promise((resolve) => (release = resolve)),
    );
    await render(Page, { data: overviewData() });
    const overview = target.querySelector(".graph-overview");
    expect(overview.hidden).toBe(false);
    expect(overview.textContent).toContain("First-paint public title");
    expect(overview.textContent).toContain("7 notes");
    expect(fetchMock).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    expect(
      target.querySelector(".interactive-graph").classList.contains("ready"),
    ).toBe(false);
    release({
      ok: true,
      headers: freshHeaders(),
      json: async () => graphFixture,
    });
    await loaded();
    await vi.waitFor(() => expect(overview.hidden).toBe(true));
    expect(
      target.querySelector(".interactive-graph").classList.contains("ready"),
    ).toBe(true);
  });

  it("retains the overview on graph failure, then removes titles at their deadline", async () => {
    fetchMock.mockResolvedValue({ ok: false });
    await render(Page, { data: overviewData(1000) });
    await vi.waitFor(() =>
      expect(target.textContent).toContain(
        "The graph is unavailable right now.",
      ),
    );
    const overview = target.querySelector(".graph-overview");
    expect(overview.hidden).toBe(false);
    expect(overview.textContent).toContain("First-paint public title");
    await vi.waitFor(
      () =>
        expect(overview.textContent).not.toContain("First-paint public title"),
      { timeout: 1500 },
    );
    expect(overview.textContent).toContain("No current snapshot is available.");
  });

  it("rejects an expired overview on hydration and preserves a fresh one when canvas is unavailable", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    await render(Page, { data: overviewData(-1) });
    expect(target.textContent).not.toContain("First-paint public title");
    await vi.waitFor(() =>
      expect(target.textContent).toContain(
        "The graph is unavailable right now.",
      ),
    );
    await unmount(instance);
    instance = undefined;
    await render(Page, { data: overviewData() });
    await vi.waitFor(() =>
      expect(target.textContent).toContain(
        "The graph is unavailable right now.",
      ),
    );
    expect(target.querySelector(".graph-overview").hidden).toBe(false);
    expect(target.textContent).toContain("First-paint public title");
  });

  it.each([1_000, 9_000_000_000_000])(
    "expires with a skewed browser clock (%s) and a backward jump",
    async (clock) => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
      vi.stubGlobal(
        "requestAnimationFrame",
        vi.fn(() => 1),
      );
      vi.stubGlobal("cancelAnimationFrame", vi.fn());
      vi.setSystemTime(clock);
      const data = overviewData(1000);
      await render(Page, { data });
      expect(target.textContent).toContain("First-paint public title");
      vi.setSystemTime(clock - 60_000);
      await vi.advanceTimersByTimeAsync(1000);
      await tick();
      expect(target.textContent).not.toContain("First-paint public title");
    },
  );

  it.each(["sleep", "backward jump"])(
    "removes titles before paint after %s without lifecycle events or advancing timers",
    async (change) => {
      const frames = new Map();
      let nextFrame = 0;
      vi.stubGlobal("requestAnimationFrame", (callback) => {
        frames.set(++nextFrame, callback);
        return nextFrame;
      });
      vi.stubGlobal("cancelAnimationFrame", (id) => frames.delete(id));
      vi.spyOn(performance, "now").mockReturnValue(1000);
      const wall = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
      await render(Page, { data: overviewData() });
      expect(target.textContent).toContain("First-paint public title");
      wall.mockReturnValue(change === "sleep" ? 1_120_000 : 999_000);
      const pending = [...frames.values()];
      frames.clear();
      for (const callback of pending) callback(1000);
      // A synchronous flush removes the titles before the browser paints.
      expect(target.textContent).not.toContain("First-paint public title");
    },
  );

  it.each(["focus", "pageshow"])(
    "revalidates the lease immediately on %s",
    async (event) => {
      vi.stubGlobal(
        "requestAnimationFrame",
        vi.fn(() => 1),
      );
      vi.stubGlobal("cancelAnimationFrame", vi.fn());
      vi.spyOn(performance, "now").mockReturnValue(1000);
      const wall = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
      await render(Page, { data: overviewData() });
      wall.mockReturnValue(1_120_000);
      window.dispatchEvent(new Event(event));
      expect(target.textContent).not.toContain("First-paint public title");
    },
  );

  it("removes titles before a back-forward cache freeze and does not revive them", async () => {
    await render(Page, { data: overviewData() });
    expect(target.textContent).toContain("First-paint public title");
    window.dispatchEvent(new Event("pagehide"));
    expect(target.textContent).not.toContain("First-paint public title");
    window.dispatchEvent(new Event("pageshow"));
    await tick();
    expect(target.textContent).not.toContain("First-paint public title");
  });

  it("removes titles before a background tab can suspend its clock", async () => {
    await render(Page, { data: overviewData() });
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(target.textContent).not.toContain("First-paint public title");
  });

  it("does not retain titles when hydration starts in an already hidden tab", async () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await render(Page, { data: overviewData() });
    expect(target.textContent).not.toContain("First-paint public title");
  });

  it("mounts real loaded graph data, sheet chrome, and unchanged keyboard controls", async () => {
    await render();
    await loaded();
    expect(target.querySelector("main.td.sheet")).not.toBeNull();
    expect(target.querySelector("h1").textContent).toBe("Public notes");
    expect(target.querySelector('a[href="/slop/factory"]').textContent).toBe(
      "Watch the factory",
    );
    expect(
      target.querySelector('.spine nav[aria-label="Site"]'),
    ).not.toBeNull();
    expect(target.textContent).toContain("7 NODES");
    expect(target.querySelector(".panel")).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/app/notes/graph",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    const input = target.querySelector("input");
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "/", bubbles: true }),
    );
    expect(document.activeElement).toBe(input);
    input.value = "Scheme";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    await tick();
    expect(input.value).toBe("");
    const filter = target.querySelector(".legend-row");
    filter.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: " ",
        bubbles: true,
        cancelable: true,
      }),
    );
    await tick();
    expect(filter.getAttribute("aria-pressed")).toBe("false");
  });

  it("opens a focus deep link, expands and navigates real note content, then closes", async () => {
    page.url.searchParams.set("focus", "sheet-note");
    await render();
    await loaded();
    await vi.waitFor(() =>
      expect(target.querySelector(".panel-body")?.textContent).toContain(
        "Synthetic browser evidence",
      ),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/app/notes/body/sheet-note",
      expect.any(Object),
    );
    expect(target.querySelector(".panel-title").textContent).toBe(
      graphFixture.nodes[0].title,
    );
    expect(target.querySelector(".panel-body pre")).not.toBeNull();
    expect(target.querySelector(".panel-body table")).not.toBeNull();
    target.querySelector('.panel-body a[data-id="tokens"]').click();
    await tick();
    await vi.waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/app/notes/body/tokens",
        expect.any(Object),
      ),
    );
    target.querySelector('button[aria-label="close"]').click();
    await tick();
    expect(target.querySelector(".panel")).toBeNull();
  });

  it("repaints the actual canvas palette on a real toggle without remounting, refetching or moving it", async () => {
    page.url.searchParams.set("focus", "sheet-note");
    await render();
    await loaded();
    const canvas = target.querySelector("canvas");
    const stage = target.querySelector(".stage");
    const day = resolveGraphPalette(stage, true);
    const transform = canvas.__zoom;
    paints.length = 0;
    target.querySelector(".scheme").click();
    // happy-dom caches ancestor selector matches after data-theme changes.
    // An unrelated class invalidates that cache without changing any token.
    target.querySelector("main.td").classList.toggle("test-style-refresh");
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    await vi.waitFor(() =>
      expect(paints.some((paint) => paint.method === "fillText")).toBe(true),
    );
    const night = resolveGraphPalette(stage, true);
    expect(night.ink).not.toBe(day.ink);
    expect(night.clusters.atom).not.toBe(day.clusters.atom);
    expect(night.selected).not.toContain("var(");
    expect(
      paints.some((p) => p.method === "fillText" && p.fill === night.ink),
    ).toBe(true);
    expect(
      paints.some((p) => p.method === "fillRect" && p.fill === night.sheet),
    ).toBe(true);
    expect(
      paints.some(
        (p) =>
          p.method === "stroke" && p.stroke === night.ink && p.alpha === 0.16,
      ),
    ).toBe(true);
    expect(
      paints.some(
        (p) => p.method === "strokeRect" && p.stroke === night.selected,
      ),
    ).toBe(true);
    expect(
      paints.some((p) => p.method === "fill" && p.fill === night.clusters.atom),
    ).toBe(true);
    expect(target.querySelector("canvas")).toBe(canvas);
    expect(canvas.__zoom).toBe(transform);
    expect(target.querySelector(".panel-title").textContent).toBe(
      graphFixture.nodes[0].title,
    );
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/app/notes/graph"),
    ).toHaveLength(1);
    target.querySelector(".scheme").click();
    target.querySelector("main.td").classList.toggle("test-style-refresh");
    await vi.waitFor(() =>
      expect(resolveGraphPalette(stage, true).ink).toBe(day.ink),
    );
  });

  it("keeps the session cache across remounts", async () => {
    await render();
    await loaded();
    await unmount(instance);
    instance = undefined;
    await render();
    await loaded();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/app/notes/graph"),
    ).toHaveLength(1);
  });

  it("renders loading, recoverable graph errors, and an empty response", async () => {
    let release;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    await render();
    expect(target.textContent).toContain("No current snapshot is available.");
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    release({ ok: false });
    await vi.waitFor(() =>
      expect(target.textContent).toContain(
        "The graph is unavailable right now.",
      ),
    );
    fetchMock.mockResolvedValueOnce({
      ok: true,
      headers: freshHeaders(),
      json: async () => ({ nodes: [], edges: [] }),
    });
    [...target.querySelectorAll("button")]
      .find((b) => b.textContent.includes("TRY AGAIN"))
      .click();
    await loaded();
    expect(target.textContent).toContain("0 NODES");
    expect(target.querySelectorAll(".legend-row")).toHaveLength(0);
  });

  it("keeps a failed expanded note closable", async () => {
    page.url.searchParams.set("focus", "sheet-note");
    fetchMock.mockImplementation(async (url) =>
      url === "/app/notes/graph"
        ? { ok: true, headers: freshHeaders(), json: async () => graphFixture }
        : { ok: false },
    );
    await render();
    await loaded();
    await vi.waitFor(() =>
      expect(target.querySelector(".panel-error")?.textContent).toBe(
        "couldn't load note body",
      ),
    );
    const close = target.querySelector('button[aria-label="close"]');
    close.focus();
    expect(document.activeElement).toBe(close);
    close.click();
    await tick();
    expect(target.querySelector(".panel")).toBeNull();
  });

  it("uses sheet tokens for touched halos and releases the theme observer on unmount", async () => {
    target.className = "td";
    await render(GraphView, {
      touched: [{ id: "sheet-note", title: "Public graph" }],
    });
    await loaded();
    const stage = target.querySelector(".stage");
    const touchedDay = resolveGraphPalette(stage, true).touched;
    paints.length = 0;
    document.documentElement.dataset.theme = "dark";
    target.classList.toggle("test-style-refresh");
    expect(resolveGraphPalette(stage, true).touched).not.toBe(touchedDay);
    await vi.waitFor(() =>
      expect(
        paints.some(
          (p) =>
            p.method === "fill" &&
            p.fill === resolveGraphPalette(stage, true).touched,
        ),
      ).toBe(true),
    );
    await unmount(instance);
    instance = undefined;
    paints.length = 0;
    document.documentElement.dataset.theme = "light";
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(paints).toHaveLength(0);
  });

  it("mounts the private consumer on the same sheet palette with its own endpoint", async () => {
    target.className = "td";
    await render(PrivatePage, {
      data: { graph: { ...graphFixture, indexed_at: null } },
    });
    await loaded();
    expect(target.querySelector(".td.notes-root")).not.toBeNull();
    expect(target.querySelector(".legend .swatch").style.background).toBe(
      "var(--warn)",
    );
    const stage = target.querySelector(".stage");
    const day = resolveGraphPalette(stage, true);
    expect(
      paints.some((p) => p.method === "fill" && p.fill === day.clusters.atom),
    ).toBe(true);
    paints.length = 0;
    document.documentElement.dataset.theme = "dark";
    target.classList.toggle("test-style-refresh");
    await vi.waitFor(() =>
      expect(
        paints.some(
          (p) =>
            p.method === "fill" &&
            p.fill === resolveGraphPalette(stage, true).clusters.atom,
        ),
      ).toBe(true),
    );
    expect(fetchMock).not.toHaveBeenCalled();
    const canvas = target.querySelector("canvas");
    canvas.dispatchEvent(
      new MouseEvent("mousedown", {
        clientX: 450,
        clientY: 320,
        bubbles: true,
      }),
    );
    canvas.dispatchEvent(
      new MouseEvent("mouseup", { clientX: 450, clientY: 320, bubbles: true }),
    );
    await tick();
    await vi.waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/knowledge/notes/sheet-note?include_review_history=true",
        expect.any(Object),
      ),
    );
    await loaded();
  });
});
