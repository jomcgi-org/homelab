import { expect, it, vi } from "vitest";
import { watchMapHealth } from "./map-errors";

function fakeMap(rendered: { source: string }[] = []) {
  const listeners = new Map<string, ((event: unknown) => void)[]>();
  return {
    rendered,
    on(type: string, listener: (event: unknown) => void) {
      listeners.set(type, [...(listeners.get(type) ?? []), listener]);
    },
    fire(type: string, event: unknown = {}) {
      for (const listener of listeners.get(type) ?? []) listener(event);
    },
    queryRenderedFeatures() {
      return this.rendered;
    },
  };
}
const tileError = {
  error: new Error("AJAXError: Failed to fetch (0)"),
  sourceId: "openmaptiles",
  tile: {},
};
const base = { source: "openmaptiles" };
const overlay = { source: "stars" };

it("keeps the map when tiles fail but the settled view renders the base map", () => {
  const map = fakeMap([base, overlay]);
  const failed = vi.fn();
  watchMapHealth(map, ["stars"], failed);
  map.fire("error", tileError);
  map.fire("idle");
  expect(failed).not.toHaveBeenCalled();
});

it("keeps the map after a slight pan that reuses rendered tiles and loses one", () => {
  const map = fakeMap([base]);
  const failed = vi.fn();
  watchMapHealth(map, ["stars"], failed);
  map.fire("idle");
  map.fire("movestart");
  map.fire("error", tileError);
  map.fire("idle");
  expect(failed).not.toHaveBeenCalled();
});

it("fails when the settled view renders no base map after tile errors", () => {
  const map = fakeMap([base]);
  const failed = vi.fn();
  watchMapHealth(map, ["stars"], failed);
  map.fire("idle");
  // Drag to an uncached area where every base-map tile fails.
  map.fire("movestart");
  map.rendered = [overlay];
  map.fire("error", tileError);
  map.fire("idle");
  expect(failed).toHaveBeenCalledOnce();
});

it("fails at once for an error without a tile", () => {
  for (const event of [
    { error: new Error("TileJSON"), sourceId: "openmaptiles" },
    { error: new Error("style") },
    {},
  ]) {
    const map = fakeMap([base]);
    const failed = vi.fn();
    watchMapHealth(map, ["stars"], failed);
    map.fire("error", event);
    expect(failed).toHaveBeenCalledOnce();
  }
});
