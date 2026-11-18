import { expect, it, vi } from "vitest";
import { watchMapHealth } from "./map-errors";

function fakeMap() {
  const listeners = new Map<string, ((event: unknown) => void)[]>();
  return {
    on(type: string, listener: (event: unknown) => void) {
      listeners.set(type, [...(listeners.get(type) ?? []), listener]);
    },
    fire(type: string, event: unknown = {}) {
      for (const listener of listeners.get(type) ?? []) listener(event);
    },
  };
}
const tileError = {
  error: new Error("AJAXError: Failed to fetch (0)"),
  sourceId: "openmaptiles",
  tile: {},
};
const baseTile = { dataType: "source", sourceId: "openmaptiles", tile: {} };

it("keeps the map when some tiles fail and base-map tiles load", () => {
  const map = fakeMap();
  const failed = vi.fn();
  watchMapHealth(map, ["stars"], failed);
  map.fire("error", tileError);
  map.fire("data", baseTile);
  map.fire("idle");
  expect(failed).not.toHaveBeenCalled();
});

it("fails when the map settles after tile errors with no base-map tile", () => {
  const map = fakeMap();
  const failed = vi.fn();
  watchMapHealth(map, ["stars"], failed);
  map.fire("error", tileError);
  // Overlay tiles alone do not count as a base map.
  map.fire("data", { ...baseTile, sourceId: "stars" });
  map.fire("idle");
  expect(failed).toHaveBeenCalledOnce();
});

it("fails at once for an error without a tile", () => {
  for (const event of [
    { error: new Error("TileJSON"), sourceId: "openmaptiles" },
    { error: new Error("style") },
    {},
  ]) {
    const map = fakeMap();
    const failed = vi.fn();
    watchMapHealth(map, ["stars"], failed);
    map.fire("error", event);
    expect(failed).toHaveBeenCalledOnce();
  }
});

it("judges each view on its own tiles after the camera moves", () => {
  const map = fakeMap();
  const failed = vi.fn();
  watchMapHealth(map, ["stars"], failed);
  map.fire("data", baseTile);
  map.fire("idle");
  expect(failed).not.toHaveBeenCalled();
  // Pan to an uncached area where every base-map tile fails.
  map.fire("movestart");
  map.fire("error", tileError);
  map.fire("idle");
  expect(failed).toHaveBeenCalledOnce();
});
