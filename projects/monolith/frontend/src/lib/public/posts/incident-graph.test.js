import { expect, test } from "vitest";
import { incidentGraph, layoutIncidentGraph } from "./incident-graph.js";
const node = (id, boundary = "evaluation") => ({
  type: "node",
  id,
  label: id,
  boundary,
  role: "process",
  detail: "Reported component",
  pages: [8],
  basis: "reported",
});
const edge = {
  type: "edge",
  from: "n1",
  to: "n2",
  label: "Shared messages",
  kind: "feedback",
  detail: "Agents exchange task information",
  pages: [9],
  basis: "reported",
};
const lines = [node("n1"), node("n2", "shared"), edge].map((x) =>
  JSON.stringify(x),
);
test("withholds partial statements and edges with unknown endpoints", () => {
  expect(incidentGraph(lines[0]).nodes).toHaveLength(0);
  expect(incidentGraph(lines.join("\n")).edges).toHaveLength(0);
  expect(incidentGraph(lines.join("\n"), true).edges).toHaveLength(1);
  expect(incidentGraph(JSON.stringify(edge) + "\n").edges).toHaveLength(0);
});
test("requires source evidence, typed relationships, and unique nodes", () => {
  const bad = [
    { ...node("bad"), pages: [99] },
    { ...node("bad"), basis: "guess" },
    { ...node("bad"), boundary: "unknown" },
    { ...node("bad"), id: undefined },
  ];
  expect(
    incidentGraph(bad.map((x) => JSON.stringify(x)).join("\n"), true).nodes,
  ).toHaveLength(0);
  expect(
    incidentGraph([lines[0], lines[0]].join("\n"), true).nodes,
  ).toHaveLength(1);
  const graph = incidentGraph(
    [...lines, JSON.stringify({ ...edge, kind: "madeup" })].join("\n"),
    true,
  );
  expect(graph.edges).toHaveLength(1);
});
test("the complete graph determines stable layout before streaming begins", () => {
  const layout = layoutIncidentGraph(incidentGraph(lines.join("\n"), true));
  expect(layout.nodes.map((n) => [n.x, n.y])).toEqual([
    [30, 64],
    [310, 64],
  ]);
  expect(layout.height).toBe(286);
});

test("a complete evidence-backed overview survives interleaved graph assembly", () => {
  const summary = {
    type: "summary",
    label: "Shared service bypassed isolation",
    detail:
      "Agents reached external systems through infrastructure intended to constrain their access.",
    pages: [8, 9],
    basis: "inferred",
  };
  const input = [
    summary,
    node("n1"),
    node("n2", "shared"),
    edge,
    node("n3", "external"),
  ]
    .map((item) => JSON.stringify(item))
    .join("\n");
  expect(incidentGraph(JSON.stringify(summary)).summary).toBeNull();
  const graph = incidentGraph(input, true);
  expect(graph.summary.label).toBe(summary.label);
  expect(graph.nodes).toHaveLength(3);
  expect(graph.edges).toHaveLength(1);
});

test("a relationship arriving before its endpoint stays hidden until that endpoint arrives", () => {
  const first =
    [node("n1"), edge].map((item) => JSON.stringify(item)).join("\n") + "\n";
  expect(incidentGraph(first).edges).toHaveLength(0);
  expect(
    incidentGraph(first + JSON.stringify(node("n2", "shared")) + "\n").edges,
  ).toHaveLength(1);
});
