// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import Precision from "./Precision.svelte";
import { percentSaved, settings } from "./data-precision.js";

test("speed bars are the share of each phase's time saved", () => {
  const by = Object.fromEntries(settings.map((s) => [s.key, s.gainPercent]));
  expect(by.dense).toBe(percentSaved(46.5, 36));
  expect(by.expert).toBe(percentSaved(16.2, 14.0));
  expect(by.attention).toBe(percentSaved(1.51, 1.27));
  expect(by.k8v6).toBe(19);
});

test("choosing a setting shows its measured trade", () => {
  const target = document.createElement("div");
  document.body.append(target);
  const c = mount(Precision, { target });
  flushSync();
  const fp8 = [...target.querySelectorAll(".seg button")].find((b) =>
    b.textContent.includes("FP8 dense"),
  );
  fp8.click();
  flushSync();
  expect(fp8.getAttribute("aria-pressed")).toBe("true");
  const readout = target.querySelector(".readout").textContent;
  expect(readout).toContain("--dense fp8");
  expect(readout).toContain("KL 0.10–0.13");
  expect(readout).toContain("86–88% top-1");
  expect(readout).toContain("1,660");
  unmount(c);
  target.remove();
});
