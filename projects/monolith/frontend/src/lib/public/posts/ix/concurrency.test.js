import { expect, test } from "vitest";
import { rows } from "./data-concurrency.js";

test("batching raises the total while each request slows", () => {
  const agg = rows.map((r) => r.batched.aggregate);
  const each = rows.map((r) => r.batched.perStream);
  expect(Math.max(...agg)).toBe(54.9);
  expect(each).toEqual([...each].sort((a, b) => b - a));
});
