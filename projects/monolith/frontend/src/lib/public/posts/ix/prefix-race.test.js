// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import PrefixRace from "./PrefixRace.svelte";
import { progress, runs } from "./data-prefix-race.js";

test("each run finishes at its measured first-token time", () => {
  const live = runs.find((r) => r.key === "live");
  expect(progress(live, 0.32)).toBeCloseTo(0.5);
  expect(progress(live, 5)).toBe(1);
  expect(runs.map((r) => r.seconds)).toEqual([13.1, 0.95, 0.64]);
});

test("the figure rests on the finished race", () => {
  const target = document.createElement("div");
  document.body.append(target);
  const c = mount(PrefixRace, { target });
  flushSync();
  const times = [...target.querySelectorAll(".time")].map((t) => t.textContent);
  expect(times).toEqual(["13.1 s", "0.95 s", "0.64 s"]);
  expect(target.textContent).toContain("1.1 GB");
  unmount(c);
  target.remove();
});
