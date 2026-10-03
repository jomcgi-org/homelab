// Render only complete streamed Mermaid statements; keep the partial line visible as source.
export function splitGeneratedAnswer(answer) {
  const fence = /```mermaid\s*\n/i.exec(answer);
  if (!fence) return { prose: answer, code: "", renderable: "" };
  const remainder = answer.slice(fence.index + fence[0].length);
  const close = remainder.indexOf("```");
  const code = close < 0 ? remainder : remainder.slice(0, close);
  const completeLines =
    close < 0 ? code.slice(0, code.lastIndexOf("\n") + 1) : code;
  return {
    prose: answer.slice(0, fence.index).trimEnd(),
    code,
    renderable: completeLines.trim(),
  };
}

// The capture asks for one plain Mermaid edge per line. Reveal only complete edges.
export function streamedGraphParts(source) {
  const nodes = new Set();
  const edges = new Set();
  for (const line of source.split("\n")) {
    const edge =
      /^\s*([A-Za-z]\w*)(?:\[[^\]]*\])?\s*-->\s*([A-Za-z]\w*)(?:\[[^\]]*\])?\s*;?\s*$/.exec(
        line,
      );
    if (!edge) continue;
    nodes.add(edge[1]);
    nodes.add(edge[2]);
    edges.add(`${edge[1]}->${edge[2]}`);
  }
  return { nodes, edges };
}
