// Build-only text equivalents. Read Mermaid's parsed graph, not its source
// syntax or SVG layout, so generated docs get the same coverage as authored docs.
import type { Diagram } from "mermaid/dist/Diagram.js";
import type { FlowDB } from "mermaid/dist/diagrams/flowchart/flowDb.js";
import type { SequenceDB } from "mermaid/dist/diagrams/sequence/sequenceDb.js";
import type { StateDB } from "mermaid/dist/diagrams/state/stateDb.js";

export function plainText(label: string) {
  const template = document.createElement("template");
  template.innerHTML = label.replace(/<br\s*\/?\s*>/gi, " ");
  return (template.content.textContent ?? "").replace(/\s+/g, " ").trim();
}

function unsupported(feature: string): never {
  throw new Error(
    `No text equivalent for ${feature}. Add Mermaid accDescr explaining its content and relationships.`,
  );
}

function flowchart(db: FlowDB) {
  const vertices = db.getVertices();
  const groups = db.getSubGraphs();
  const names = new Map([
    ...[...vertices].map(
      ([id, node]) => [id, plainText(node.text ?? id)] as const,
    ),
    ...groups.map((group) => [group.id, plainText(group.title)] as const),
  ]);
  const name = (id: string) => names.get(id) ?? unsupported(`node ${id}`);
  return [
    `Nodes: ${[...vertices.keys()].map(name).join("; ")}.`,
    ...groups.map(
      (group) =>
        `Group ${name(group.id)} contains: ${group.nodes.map(name).join("; ")}.`,
    ),
    ...[...vertices.values()]
      .filter((node) => node.type === "diamond")
      .map((node) => `Decision: ${name(node.id)}.`),
    ...db
      .getEdges()
      .filter((edge) => edge.stroke !== "invisible")
      .map((edge) => {
        const from = name(edge.start);
        const to = name(edge.end);
        const connection =
          edge.type === "arrow_point"
            ? `${from} points to ${to}`
            : edge.type === "double_arrow_point"
              ? `${from} and ${to} connect in both directions`
              : edge.type === "arrow_open"
                ? `${from} connects with ${to} (no direction)`
                : unsupported(`flowchart edge ${edge.type}`);
        const stroke =
          edge.stroke === "dotted"
            ? " (dashed)"
            : edge.stroke === "thick"
              ? " (thick)"
              : "";
        const label = plainText(edge.text);
        return `${connection}${stroke}${label ? `: ${label}` : ""}.`;
      }),
  ];
}

function sequence(db: SequenceDB) {
  const actors = db.getActors();
  const name = (id: string) => {
    const actor = actors.get(id);
    return actor
      ? plainText(actor.description || actor.name)
      : unsupported(`participant ${id}`);
  };
  // More complex sequences need an authored description until their branching
  // and lifetime semantics can be represented faithfully here.
  if (
    db.getBoxes().length ||
    db.getCreatedActors().size ||
    db.getDestroyedActors().size
  )
    unsupported("sequence groups or participant lifetimes");
  return [
    `Participants: ${[...actors.keys()].map(name).join("; ")}. Messages in order:`,
    ...db.getMessages().map((message, index) => {
      if (
        ![db.LINETYPE.SOLID, db.LINETYPE.DOTTED].includes(
          message.type as 0 | 1,
        ) ||
        !message.from ||
        !message.to ||
        typeof message.message !== "string" ||
        message.activate
      )
        unsupported(`sequence message type ${message.type}`);
      const stroke =
        message.type === db.LINETYPE.DOTTED ? " (dashed arrow)" : "";
      return `${index + 1}. ${name(message.from)} sends to ${name(message.to)}${stroke}: ${plainText(message.message)}.`;
    }),
  ];
}

function states(db: StateDB) {
  const { nodes, edges } = db.getData();
  const names = new Map(
    nodes.map((node) => {
      if (
        node.isGroup ||
        node.parentId ||
        !["rect", "stateStart", "stateEnd"].includes(node.shape)
      )
        unsupported(`state shape ${node.shape} or nested state`);
      return [
        node.id,
        node.shape === "stateStart"
          ? "Start"
          : node.shape === "stateEnd"
            ? "End"
            : plainText(
                Array.isArray(node.label)
                  ? node.label.join("; ")
                  : (node.label ?? node.id),
              ),
      ];
    }),
  );
  const name = (id: string) => names.get(id) ?? unsupported(`state ${id}`);
  return [
    `States: ${nodes.map((node) => name(node.id)).join("; ")}.`,
    ...edges.map((edge) => {
      const label = plainText(edge.label ?? "");
      return `${name(edge.start)} transitions to ${name(edge.end)}${label ? `: ${label}` : ""}.`;
    }),
  ];
}

export function describeDiagram(diagram: Diagram) {
  // Authors can supply a richer explanation, including for other diagram types.
  const authored = plainText(diagram.db.getAccDescription?.() ?? "");
  if (authored) return { kind: "Diagram", statements: [authored] };
  switch (diagram.type) {
    case "flowchart-v2":
      return { kind: "Flowchart", statements: flowchart(diagram.db as FlowDB) };
    case "sequence":
      return {
        kind: "Sequence diagram",
        statements: sequence(diagram.db as SequenceDB),
      };
    case "stateDiagram":
      return {
        kind: "State diagram",
        statements: states(diagram.db as StateDB),
      };
    default:
      return unsupported(`diagram type ${diagram.type}`);
  }
}
