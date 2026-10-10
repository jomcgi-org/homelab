// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import Fit from "./Fit.svelte";
import { capacities, totalGb, weights, widthPct } from "./data-fit.js";

test("the parts add up to the release inventory and share one scale", () => {
  expect(totalGb()).toBe(134.26);
  expect(widthPct(totalGb())).toBe(100);
  expect(widthPct(capacities.find((c) => c.key === "both").gb)).toBeCloseTo(
    65.54,
    2,
  );
});

test("selecting a part explains where it lives", () => {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Fit, { target });
  flushSync();
  const readout = target.querySelector(".readout");
  expect(readout.textContent).toContain("Decoder experts, 67.95 GB");
  const ple = [...target.querySelectorAll(".legend button")].find((b) =>
    b.textContent.includes("PLE"),
  );
  ple.click();
  flushSync();
  expect(readout.textContent).toContain("PLE tables, 51.2 GB");
  expect(ple.getAttribute("aria-pressed")).toBe("true");
  expect(target.querySelectorAll(".seg-part")).toHaveLength(weights.length);
  unmount(component);
  target.remove();
});
