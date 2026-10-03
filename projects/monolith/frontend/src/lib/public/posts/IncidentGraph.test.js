// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { mount, tick, unmount } from "svelte";
import IncidentGraph from "./IncidentGraph.svelte";
import recording from "./qwen-replay.json";
import { incidentGraph } from "./incident-graph.js";
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
  const graph = incidentGraph(answer, true);
  if (graph.summary)
    expect(target.querySelector(".takeaway").textContent).toBe(
      graph.summary.detail,
    );
  const reviewedEdges = graph.edges.map((edge) => ({
    ...edge,
    ...recording.review?.[edge.id],
  }));
  const monitor = graph.nodes.find((node) => node.role === "monitor");
  const alertEdge = reviewedEdges.find((edge) => edge.from === monitor.id);
  const alert = [...target.querySelectorAll(".edge-hit")].find(
    (el) =>
      el.getAttribute("aria-label") ===
      `${alertEdge.label}: ${alertEdge.detail}`,
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
  const failure = reviewedEdges.find(
    (edge) => edge.kind === "failure" && edge.basis === "inferred",
  );
  [...target.querySelectorAll(".edge-hit")]
    .find(
      (el) =>
        el.getAttribute("aria-label") === `${failure.label}: ${failure.detail}`,
    )
    .dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await tick();
  expect(target.querySelector(".graph-detail a").textContent).toContain(
    "Analysis",
  );
  expect(target.querySelector("animateMotion")).toBeNull();
  await unmount(component);
  target.remove();
});
