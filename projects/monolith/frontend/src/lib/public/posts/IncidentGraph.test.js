// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { mount, tick, unmount } from "svelte";
import IncidentGraph from "./IncidentGraph.svelte";
import recording from "./qwen-replay.json";
test("clicking a connection exposes its verified source and inferred analysis", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const answer = recording.turns[0].events.map((e) => e.content).join("");
  const component = mount(IncidentGraph, {
    target,
    props: {
      answer,
      finalAnswer: answer,
      complete: true,
      sourceUrl: recording.source.url,
      review: recording.review,
    },
  });
  await tick();
  const alert = target.querySelector(
    '[aria-label="Trigger alert: Monitoring notifies team"]',
  );
  alert.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await tick();
  expect(
    target.querySelector(".graph-detail a").getAttribute("href"),
  ).toContain("#page=15");
  expect(
    alert
      .closest(".connection")
      .querySelector(".edge")
      .classList.contains("feedback"),
  ).toBe(true);
  target
    .querySelector('[aria-label="Escape isolation: Sandbox controls failed"]')
    .dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await tick();
  expect(target.querySelector(".graph-detail a").textContent).toContain(
    "Analysis",
  );
  await unmount(component);
  target.remove();
});
