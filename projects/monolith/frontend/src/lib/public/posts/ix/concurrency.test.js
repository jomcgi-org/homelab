// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import Concurrency from "./Concurrency.svelte";
import { rows } from "./data-concurrency.js";

test("batching raises the total while each request slows", () => {
  const agg = rows.map((r) => r.batched.aggregate);
  const each = rows.map((r) => r.batched.perStream);
  expect(Math.max(...agg)).toBe(54.9);
  expect(each).toEqual([...each].sort((a, b) => b - a));
});

test("choosing a request count updates the readout", () => {
  const target = document.createElement("div");
  document.body.append(target);
  const c = mount(Concurrency, { target });
  flushSync();
  const four = [...target.querySelectorAll(".seg button")].find(
    (b) => b.textContent === "4",
  );
  four.click();
  flushSync();
  const readout = target.querySelector(".readout").textContent;
  expect(readout).toContain("54.9");
  expect(readout).toContain("16.9");
  expect(readout).toContain("6.7 / 9.9 s");
  unmount(c);
  target.remove();
});
