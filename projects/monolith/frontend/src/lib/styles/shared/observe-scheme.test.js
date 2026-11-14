import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { observeScheme } from "./observe-scheme.js";
import { watchMapScheme } from "../../trips/map-theme.js";
import { watchGraphPalette } from "../../components/notes/canvas-palette.js";

let observers;
let listeners;
let root;
let stage;
let media;
let probes;

beforeEach(() => {
  observers = new Set();
  listeners = new Set();
  probes = [];
  root = {};
  stage = { append: vi.fn() };
  media = {
    addEventListener: vi.fn((type, callback) => {
      expect(type).toBe("change");
      listeners.add(callback);
    }),
    removeEventListener: vi.fn((type, callback) => {
      expect(type).toBe("change");
      listeners.delete(callback);
    }),
  };
  vi.stubGlobal(
    "MutationObserver",
    class {
      constructor(callback) {
        this.callback = callback;
      }
      observe(target, options) {
        expect(target).toBe(root);
        expect(options).toEqual({
          attributes: true,
          attributeFilter: ["data-theme"],
        });
        this.target = target;
        this.options = options;
        observers.add(this);
      }
      disconnect() {
        observers.delete(this);
      }
    },
  );
  vi.stubGlobal("document", {
    documentElement: root,
    createElement: vi.fn((tag) => {
      expect(tag).toBe("span");
      const probe = { style: {}, remove: vi.fn() };
      probes.push(probe);
      return probe;
    }),
  });
  vi.stubGlobal("getComputedStyle", (probe) => ({
    color: `resolved:${probe.style.color}`,
  }));
  vi.stubGlobal("window", {
    matchMedia: vi.fn((query) => {
      expect(query).toBe("(prefers-color-scheme: dark)");
      return media;
    }),
  });
});

afterEach(() => vi.unstubAllGlobals());

function mutate(attributeName) {
  const records = [{ type: "attributes", target: root, attributeName }];
  for (const observer of observers) {
    if (
      observer.options.attributes &&
      observer.options.attributeFilter.includes(attributeName)
    ) {
      observer.callback(records, observer);
    }
  }
  return records;
}

function changeMedia() {
  const event = { type: "change", matches: true };
  for (const callback of listeners) callback.call(media, event);
  return event;
}

const subscriptions = [
  ["observeScheme", observeScheme, 0],
  ["watchMapScheme", watchMapScheme, 1],
  ["watchGraphPalette", (update) => watchGraphPalette(stage, update), 0],
];

describe.each(subscriptions)("%s", (name, subscribe, initial) => {
  it("preserves initial counts, filters attributes and disposes both event sources", () => {
    const repaint = vi.fn();
    const dispose = subscribe(repaint);
    expect(repaint).toHaveBeenCalledTimes(initial);
    if (initial) expect(repaint).toHaveBeenLastCalledWith();
    mutate("class");
    mutate("aria-label");
    expect(repaint).toHaveBeenCalledTimes(initial);
    const observer = [...observers][0];
    const records = mutate("data-theme");
    expect(repaint).toHaveBeenCalledTimes(initial + 1);
    if (name !== "watchGraphPalette") {
      expect(repaint).toHaveBeenLastCalledWith(records, observer);
    }
    const event = changeMedia();
    expect(repaint).toHaveBeenCalledTimes(initial + 2);
    if (name !== "watchGraphPalette") {
      expect(repaint).toHaveBeenLastCalledWith(event);
    } else {
      // The real resolver must read the supplied stage and resolve sheet tokens
      // on each event, rather than returning the private legacy palette.
      expect(stage.append).toHaveBeenCalledTimes(2);
      expect(stage.append.mock.calls.map(([probe]) => probe)).toEqual(probes);
      expect(
        probes.every((probe) => probe.remove.mock.calls.length === 1),
      ).toBe(true);
      expect(repaint).toHaveBeenLastCalledWith(
        expect.objectContaining({
          ink: "resolved:var(--ink)",
          sheet: "resolved:var(--sheet)",
          selected: "resolved:var(--accent-ink)",
        }),
      );
      expect(repaint.mock.calls.every((args) => args.length === 1)).toBe(true);
    }
    dispose();
    expect(observers.size).toBe(0);
    expect(listeners.size).toBe(0);
    mutate("data-theme");
    changeMedia();
    expect(repaint).toHaveBeenCalledTimes(initial + 2);
  });

  it("gives each subscriber independent ownership", () => {
    const first = vi.fn();
    const second = vi.fn();
    const stopFirst = subscribe(first);
    const stopSecond = subscribe(second);
    expect(observers.size).toBe(2);
    expect(listeners.size).toBe(2);
    mutate("data-theme");
    changeMedia();
    expect(first).toHaveBeenCalledTimes(initial + 2);
    expect(second).toHaveBeenCalledTimes(initial + 2);
    stopFirst();
    expect(observers.size).toBe(1);
    expect(listeners.size).toBe(1);
    mutate("data-theme");
    changeMedia();
    expect(first).toHaveBeenCalledTimes(initial + 2);
    expect(second).toHaveBeenCalledTimes(initial + 4);
    stopSecond();
    expect(observers.size).toBe(0);
    expect(listeners.size).toBe(0);
  });
});

it("imports complete shared and adapter modules without browser globals", async () => {
  vi.resetModules();
  for (const name of ["window", "document", "MutationObserver"]) {
    vi.stubGlobal(name, undefined);
  }
  expect((await import("./observe-scheme.js")).observeScheme).toBeTypeOf(
    "function",
  );
  expect((await import("../../trips/map-theme.js")).watchMapScheme).toBeTypeOf(
    "function",
  );
  expect(
    (await import("../../components/notes/canvas-palette.js"))
      .watchGraphPalette,
  ).toBeTypeOf("function");
});
