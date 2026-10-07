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
  // The monitoring loop's feedback edge (into or out of the monitor, depending
  // on how the recorded graph draws it).
  const alertEdge = reviewedEdges.find((edge) => edge.kind === "feedback");
  const alert = [...target.querySelectorAll(".edge-hit")].find(
    (el) =>
      el.getAttribute("aria-label") ===
      `${alertEdge.label}: ${alertEdge.detail}`,
  );
  alert.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await tick();
  expect(
    target.querySelector(".graph-detail a").getAttribute("href"),
  ).toContain(`#page=${alertEdge.pages[0]}`);
  expect(
    alert
      .closest(".connection")
      .querySelector(".edge")
      .classList.contains("feedback"),
  ).toBe(true);
  // A failure edge's citation says whether it is reported by the source or the
  // model's own analysis (an inferred edge).
  const failure =
    reviewedEdges.find(
      (edge) => edge.kind === "failure" && edge.basis === "inferred",
    ) ?? reviewedEdges.find((edge) => edge.kind === "failure");
  [...target.querySelectorAll(".edge-hit")]
    .find(
      (el) =>
        el.getAttribute("aria-label") === `${failure.label}: ${failure.detail}`,
    )
    .dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await tick();
  expect(target.querySelector(".graph-detail a").textContent).toContain(
    failure.basis === "inferred" ? "Analysis" : "Report p.",
  );
  expect(target.querySelector("animateMotion")).toBeNull();
  await unmount(component);
  target.remove();
});
