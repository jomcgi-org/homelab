import { expect, test } from "vitest";
import { capacities, totalGb, weights, widthPct } from "./data-fit.js";

test("the parts add up to the release inventory and share one scale", () => {
  expect(totalGb()).toBe(134.26);
  expect(widthPct(totalGb())).toBe(100);
  expect(widthPct(capacities.find((c) => c.key === "both").gb)).toBeCloseTo(
    65.54,
    2,
  );
});
