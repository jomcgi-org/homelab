// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import CopyQueue from "./CopyQueue.svelte";
import {
  PIECE_RECORDS,
  RECORD_MS,
  STAGE_MS,
  STAGE_RECORDS,
  simulate,
} from "./data-copy-queue.js";

test("all at once: this layer's copies wait behind the whole stage-ahead", () => {
  const run = simulate("all", 4.3, 10);
  expect(run.wait).toBeCloseTo(STAGE_MS - 4.3);
  const fetch = run.blocks.find((b) => b.kind === "fetch");
  expect(fetch.start).toBe(STAGE_MS);
});

test("trickle: the wait is at most one piece and every record is still copied", () => {
  for (const arrive of [0, 0.3, 4.3, 17, 44.9]) {
    const run = simulate("trickle", arrive, 10);
    expect(run.wait).toBeGreaterThanOrEqual(0);
    expect(run.wait).toBeLessThanOrEqual(PIECE_RECORDS * RECORD_MS + 1e-9);
    const staged = run.blocks
      .filter((b) => b.kind === "stage")
      .reduce((n, b) => n + (b.end - b.start) / RECORD_MS, 0);
    expect(staged).toBeCloseTo(STAGE_RECORDS);
  }
});

test("the figure renders and switches mode", () => {
  const target = document.createElement("div");
  document.body.append(target);
  const c = mount(CopyQueue, { target });
  flushSync();
  const label = () => target.querySelector("svg").getAttribute("aria-label");
  expect(label()).toContain("41 ms");
  [...target.querySelectorAll("button")]
    .find((b) => b.textContent.includes("at a time"))
    .click();
  flushSync();
  expect(label()).toContain("under 1 ms");
  unmount(c);
});
