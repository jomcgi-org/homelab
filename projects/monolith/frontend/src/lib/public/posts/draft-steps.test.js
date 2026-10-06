import { expect, test } from "vitest";
import { decodeSteps, rollingRate } from "./draft-steps.js";

const events = (ats) => ats.map((at) => ({ at, content: "x" }));

test("tokens arriving together are one step, classified by size", () => {
  const steps = decodeSteps(events([0, 40, 41, 80, 81, 82, 82, 83, 120]));
  expect(steps.map((s) => [s.at, s.tokens, s.source])).toEqual([
    [0, 1, "single"],
    [40, 2, "draft"],
    [80, 5, "lookup"],
    [120, 1, "single"],
  ]);
});

test("the rolling rate rises where a lookup step lands", () => {
  const steps = decodeSteps(events([0, 40, 80, 120, 160, 161, 162, 163, 164, 165]));
  const rates = rollingRate(steps, 100);
  const before = rates.find((r) => r.at === 120).rate;
  const after = rates.find((r) => r.at === 160).rate;
  expect(after).toBeGreaterThan(2 * before);
});

test("text chunks carrying several tokens count every token", () => {
  const steps = decodeSteps([
    { at: 0, content: "a", tokens: 1 },
    { at: 40, content: "bc", tokens: 2 },
    { at: 41, content: "d", tokens: 1 },
  ]);
  expect(steps.map((s) => [s.tokens, s.source])).toEqual([
    [1, "single"],
    [3, "lookup"],
  ]);
});
