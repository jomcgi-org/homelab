import { expect, test } from "vitest";
import { runs } from "./data-prefix-race.js";

test("first-token times are the measured ones, slowest first", () => {
  expect(runs.map((r) => r.seconds)).toEqual([13.1, 0.95, 0.64]);
});
