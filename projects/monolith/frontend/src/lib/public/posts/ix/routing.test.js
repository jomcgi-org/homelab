// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { mount, tick, unmount } from "svelte";
import Routing from "./Routing.svelte";
import { EXPERTS, PER_TOKEN, routedExperts } from "./data-routing.js";

test("each token routes to ten distinct experts, the same ones every render", () => {
  for (const n of [1, 2, 3, 50]) {
    const picks = routedExperts(n);
    expect(new Set(picks).size).toBe(PER_TOKEN);
    expect(picks.every((i) => i >= 0 && i < EXPERTS)).toBe(true);
    expect(routedExperts(n)).toEqual(picks);
  }
  expect(routedExperts(2)).not.toEqual(routedExperts(1));
});

test("Next token lights a different ten of 512 cells", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Routing, { target });
  await tick();
  const lit = () =>
    [...target.querySelectorAll(".pool i")]
      .map((c, i) => (c.classList.contains("on") ? i : -1))
      .filter((i) => i >= 0);
  expect(target.querySelectorAll(".pool i")).toHaveLength(512);
  const first = lit();
  expect(first).toEqual(routedExperts(1));
  target.querySelector("button.next").click();
  await tick();
  expect(lit()).toEqual(routedExperts(2));
  unmount(component);
  target.remove();
});
