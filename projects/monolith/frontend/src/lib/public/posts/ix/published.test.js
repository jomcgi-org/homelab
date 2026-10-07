// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import Published from "./Published.svelte";
import { pct, published, rate } from "./data-published.js";

test("rates format single values and ranges, on a 0-200 scale", () => {
  expect(rate(published.find((r) => r.key === "vllm"))).toBe("74.4 tok/s");
  expect(rate(published.find((r) => r.key === "sglang-later"))).toBe(
    "179.4–192.7 tok/s",
  );
  expect(pct(46)).toBe(23);
  for (const r of published) expect(r.to).toBeLessThanOrEqual(200);
});

test("the 4090 row starts selected and each row shows its caveat", () => {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Published, { target });
  flushSync();
  const readout = target.querySelector(".readout");
  expect(readout.textContent).toContain("oom-inference, 4090, 45.4 tok/s");
  expect(readout.querySelector("a")).toBeNull();
  const tuned = [...target.querySelectorAll(".row")].find((b) =>
    b.textContent.includes("tuned"),
  );
  tuned.click();
  flushSync();
  expect(readout.textContent).toContain("not all of the gain is MTP");
  expect(readout.querySelector("a").href).toContain("lEWFkRAD");
  unmount(component);
  target.remove();
});
