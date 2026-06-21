import { test } from "node:test";
import assert from "node:assert/strict";
import { isChanged } from "./diff.mjs";

test("sub-threshold noise is not a change", () => {
  assert.equal(isChanged({ mismatched: 30, total: 1_000_000 }), false);
});
test("clear change is reported", () => {
  assert.equal(isChanged({ mismatched: 5000, total: 1_000_000 }), true);
});
test("missing baseline counts as added/changed", () => {
  assert.equal(isChanged({ added: true }), true);
});
