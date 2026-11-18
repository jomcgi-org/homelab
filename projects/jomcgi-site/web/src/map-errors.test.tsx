import { expect, it } from "vitest";
import { isMapFailure } from "./map-errors";

it("treats a single tile or source error as non-fatal", () => {
  const error = new Error("AJAXError: Failed to fetch (0)");
  expect(isMapFailure({ error, sourceId: "openmaptiles", tile: {} })).toBe(
    false,
  );
  expect(isMapFailure({ error, sourceId: "openmaptiles" })).toBe(false);
  expect(isMapFailure({ error, tile: {} })).toBe(false);
});

it("treats style and WebGL errors as a failed map", () => {
  expect(isMapFailure({ error: new Error("style") })).toBe(true);
  expect(isMapFailure({})).toBe(true);
});
