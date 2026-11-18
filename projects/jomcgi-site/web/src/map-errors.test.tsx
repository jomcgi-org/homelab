import { expect, it } from "vitest";
import { isMapFailure } from "./map-errors";

it("treats a single failed or cancelled tile as non-fatal", () => {
  const error = new Error("AJAXError: Failed to fetch (0)");
  expect(isMapFailure({ error, sourceId: "openmaptiles", tile: {} })).toBe(
    false,
  );
  expect(isMapFailure({ error, tile: {} })).toBe(false);
});

it("treats a source error without a tile, style and WebGL errors as a failed map", () => {
  // A failed TileJSON request names the source but no tile.
  expect(
    isMapFailure({ error: new Error("TileJSON"), sourceId: "openmaptiles" }),
  ).toBe(true);
  expect(isMapFailure({ error: new Error("style") })).toBe(true);
  expect(isMapFailure({})).toBe(true);
});
