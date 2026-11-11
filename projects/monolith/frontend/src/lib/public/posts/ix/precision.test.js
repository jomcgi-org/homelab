import { expect, test } from "vitest";
import { settings } from "./data-precision.js";

test("speed bars are the share of each phase's time saved", () => {
  const by = Object.fromEntries(settings.map((s) => [s.key, s.gainPercent]));
  // Rounded savings from 46.5 to 36 ms, 16.2 to 14 ms and 1.51 to 1.27 s.
  expect(by.dense).toBe(23);
  expect(by.expert).toBe(14);
  expect(by.attention).toBe(16);
  expect(by.k8v6).toBe(19);
});
