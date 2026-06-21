import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveFixture } from "./mock-server.mjs";

test("maps known api path to fixture", () => {
  assert.equal(
    resolveFixture("/api/stars/sites"),
    "fixtures/api/stars_sites.json",
  );
});
test("maps parameterized trip path", () => {
  assert.equal(
    resolveFixture("/api/trips/trip/demo-trip"),
    "fixtures/api/trips_trip.json",
  );
});
test("returns null for unknown path", () => {
  assert.equal(resolveFixture("/api/does/not/exist"), null);
});
