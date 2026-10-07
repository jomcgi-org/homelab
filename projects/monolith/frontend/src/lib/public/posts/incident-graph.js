const boundaries = ["evaluation", "shared", "external"];
const roles = ["controller", "process", "monitor"];
const kinds = ["action", "feedback", "failure"];
function evidence(item) {
  return (
    ["reported", "inferred"].includes(item.basis) &&
    typeof item.label === "string" &&
    item.label.length > 0 &&
    item.label.length <= 80 &&
    typeof item.detail === "string" &&
    item.detail.length <= 240 &&
    Array.isArray(item.pages) &&
    item.pages.length > 0 &&
    item.pages.every((p) => Number.isInteger(p) && p >= 1 && p <= 38)
  );
}
// A complete model statement is required before a node or connection can appear.
export function incidentGraph(answer, complete = false) {
  const lines = answer.split("\n");
  if (!complete) lines.pop();
  let summary = null;
  const nodes = [],
    edges = [];
  for (const line of lines) {
    try {
      const item = JSON.parse(line);
      if (!evidence(item)) continue;
      if (item.type === "summary" && !summary) {
        summary = { ...item, id: "summary" };
      } else if (
        item.type === "node" &&
        nodes.length < 12 &&
        typeof item.id === "string" &&
        /^[a-zA-Z][\w-]{0,30}$/.test(item.id) &&
        boundaries.includes(item.boundary) &&
        roles.includes(item.role) &&
        !nodes.some((n) => n.id === item.id)
      ) {
        nodes.push(item);
      } else if (
        item.type === "edge" &&
        edges.length < 20 &&
        kinds.includes(item.kind) &&
        item.from !== item.to &&
        !edges.some(
          (e) =>
            e.from === item.from && e.to === item.to && e.kind === item.kind,
        )
      ) {
        edges.push({ ...item, id: `${item.from}-${item.to}-${item.kind}` });
      }
    } catch {
      /* Wait for the next complete statement. */
    }
  }
  return {
    nodes,
    edges: edges.filter(
      (edge) =>
        nodes.some((node) => node.id === edge.from) &&
        nodes.some((node) => node.id === edge.to),
    ),
    summary,
  };
}
export function layoutIncidentGraph(graph) {
  const rows = [0, 0, 0];
  const nodes = graph.nodes.map((node) => {
    const column = boundaries.indexOf(node.boundary);
    // Rows start below a 24-unit band that carries long edges between lanes.
    return { ...node, x: column * 280 + 30, y: rows[column]++ * 105 + 88 };
  });
  return { nodes, height: Math.max(2, ...rows) * 105 + 100 };
}
