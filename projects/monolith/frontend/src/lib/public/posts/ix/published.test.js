import { expect, test } from "vitest";
import { pct, published, rate } from "./data-published.js";

test("rates format single values and ranges, on a 0-200 scale", () => {
  expect(rate(published.find((r) => r.key === "vllm"))).toBe("74.4 tok/s");
  expect(rate(published.find((r) => r.key === "sglang-later"))).toBe(
    "179.4–192.7 tok/s",
  );
  expect(pct(46)).toBe(23);
  for (const r of published) expect(r.to).toBeLessThanOrEqual(200);
});
