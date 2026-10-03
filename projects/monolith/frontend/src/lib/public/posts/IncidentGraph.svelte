<script>
  import { incidentGraph, layoutIncidentGraph } from "./incident-graph.js";
  let {
    answer = "",
    finalAnswer = "",
    complete = false,
    sourceUrl = "",
    review = {},
  } = $props();
  const instance = `incident-${Math.random().toString(36).slice(2)}`;
  let captured = $derived(incidentGraph(answer, complete));
  // Keep the captured response intact; use verified page links and relationship types in the view.
  let graph = $derived({
    summary: captured.summary,
    nodes: captured.nodes.map((item) => ({ ...item, ...review[item.id] })),
    edges: captured.edges.map((item) => ({ ...item, ...review[item.id] })),
  });
  let layout = $derived(layoutIncidentGraph(incidentGraph(finalAnswer, true)));
  let selected = $state(null);
  let active = $derived(
    [...graph.nodes, ...graph.edges].find((item) => item.id === selected) ??
      graph.summary ??
      graph.edges.find((edge) => edge.kind === "failure") ??
      graph.nodes[0],
  );
  function path(edge, index) {
    const from = layout.nodes.find((n) => n.id === edge.from);
    const to = layout.nodes.find((n) => n.id === edge.to);
    if (!from || !to) return "";
    const down = to.y > from.y;
    if (from.x === to.x) {
      const x = from.x + 105;
      return `M${x},${from.y + (down ? 62 : 0)} L${x},${to.y + (down ? 0 : 62)}`;
    }
    const right = to.x > from.x;
    const x1 = from.x + (right ? 210 : 0);
    const x2 = to.x + (right ? 0 : 210);
    const y1 = from.y + 31;
    const y2 = to.y + 31;
    const direction = right ? 1 : -1;
    if (y1 === y2 && Math.abs(to.x - from.x) === 280) {
      return `M${x1},${y1} L${x2},${y2}`;
    }
    const points =
      Math.abs(to.x - from.x) > 280
        ? [
            [x1, y1],
            [x1 + direction * 24, y1],
            [x1 + direction * 24, 43 - (index % 3) * 8],
            [x2 - direction * 24, 43 - (index % 3) * 8],
            [x2 - direction * 24, y2],
            [x2, y2],
          ]
        : [
            [x1, y1],
            [(x1 + x2) / 2 + direction * 12, y1],
            [(x1 + x2) / 2 + direction * 12, y2],
            [x2, y2],
          ];
    let result = `M${points[0].join(",")}`;
    for (let i = 1; i < points.length - 1; i++) {
      const previous = points[i - 1],
        current = points[i],
        next = points[i + 1];
      const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
      const before = distance(previous, current),
        after = distance(current, next);
      const radius = Math.min(7, before / 2, after / 2);
      const entry = current.map(
        (v, axis) => v + ((previous[axis] - v) * radius) / before,
      );
      const exit = current.map(
        (v, axis) => v + ((next[axis] - v) * radius) / after,
      );
      result += ` L${entry.join(",")} Q${current.join(",")} ${exit.join(",")}`;
    }
    return `${result} L${points.at(-1).join(",")}`;
  }
  const tone = (item) =>
    item.kind === "failure"
      ? "disk"
      : item.kind === "feedback" || item.role === "monitor"
        ? "ram"
        : "gpu";
  const choose = (item) => (selected = selected === item.id ? null : item.id);
</script>

<div class="incident-graph">
  <div class="graph-scroll">
    <div class="graph-canvas" style={`aspect-ratio:840 / ${layout.height}`}>
      <div class="boundaries" aria-hidden="true">
        <span>Evaluation</span><span>Shared infrastructure</span><span
          >External systems</span
        >
      </div>
      <svg
        viewBox={`0 0 840 ${layout.height}`}
        role="group"
        aria-label="Incident control actions, feedback and failed boundaries"
      >
        <defs
          >{#each ["gpu", "ram", "disk"] as color}<marker
              id={`${instance}-${color}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto"
              ><path
                d="M0 0 L10 5 L0 10 z"
                fill={`var(--tone-${color})`}
              /></marker
            >{/each}</defs
        >
        {#each graph.edges as edge, i (edge.id)}
          <g
            class="connection"
            class:chosen={active?.id === edge.id ||
              (active?.type === "summary" && edge.kind === "failure")}
            style={`--edge-tone:var(--tone-${tone(edge)})`}
          >
            <path
              class="edge"
              class:feedback={edge.kind === "feedback"}
              d={path(edge, i)}
              marker-end={`url(#${instance}-${tone(edge)})`}
            />
            <path
              class="edge-hit"
              d={path(edge, i)}
              role="button"
              tabindex="0"
              aria-label={`${edge.label}: ${edge.detail}`}
              aria-pressed={active?.id === edge.id}
              onclick={() => choose(edge)}
              onkeydown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  choose(edge);
                }
              }}
            />
          </g>
        {/each}
      </svg>
      {#each layout.nodes as node (node.id)}
        {#if graph.nodes.some((n) => n.id === node.id)}
          <button
            class="graph-node"
            class:chosen={active?.id === node.id}
            style={`left:${(node.x / 840) * 100}%;top:${(node.y / layout.height) * 100}%;height:${(62 / layout.height) * 100}%;--node-tone:var(--tone-${tone(node)})`}
            onclick={() => choose(node)}
            aria-pressed={active?.id === node.id}
          >
            <span class="role">{node.role}</span><strong>{node.label}</strong>
          </button>
        {/if}
      {/each}
      <p class="interaction-hint">↗ Click a node or arrow to explore</p>
    </div>
  </div>
  <div class="graph-detail" aria-live="polite">
    {#if active?.type === "summary"}
      <p class="takeaway">{active.detail}</p>
      <a
        class="summary-source"
        href={`${sourceUrl}#page=${active.pages[0]}`}
        target="_blank"
        rel="noreferrer">Analysis · Report p. {active.pages.join(", ")}</a
      >
    {:else if active}<div class="detail-top">
        <strong>{active.label}</strong><a
          href={`${sourceUrl}#page=${active.pages[0]}`}
          target="_blank"
          rel="noreferrer"
          >{active.basis === "inferred" ? "Analysis · " : ""}Report p. {active.pages.join(
            ", ",
          )}</a
        >
      </div>
      <p>{active.detail}</p>{:else}<p class="waiting">
        Building the control graph…
      </p>{/if}
  </div>

  <details>
    <summary>Model output</summary>
    <pre>{answer}</pre>
  </details>
</div>

<style>
  .incident-graph {
    min-width: 0;
  }
  .graph-scroll {
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scrollbar-width: thin;
    scrollbar-color: var(--tone-gpu) var(--line);
  }
  .graph-scroll::-webkit-scrollbar {
    height: 7px;
  }
  .graph-scroll::-webkit-scrollbar-thumb {
    background: var(--tone-gpu);
  }
  .graph-canvas {
    position: relative;
    width: 100%;
    max-width: 840px;
    min-width: 638px;
  }
  .interaction-hint {
    position: absolute;
    bottom: 10px;
    left: 30px;
    margin: 0;
    color: var(--tone-gpu);
    font: 0.7rem var(--font-code);
  }
  .boundaries {
    position: absolute;
    inset: 0;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    pointer-events: none;
  }
  .boundaries span {
    border-right: 1px dashed var(--line);
    padding: 20px 30px;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .boundaries span:last-child {
    border: 0;
  }
  svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    overflow: visible;
  }
  .edge {
    fill: none;
    stroke: var(--edge-tone);
    stroke-width: 2;
    opacity: 0.7;
    animation: connect 0.4s ease-out;
  }
  .feedback {
    stroke-dasharray: 5 5;
  }
  .edge-hit {
    fill: none;
    stroke: transparent;
    stroke-width: 16;
    cursor: pointer;
  }
  .edge-hit:focus-visible {
    stroke: var(--edge-tone);
    stroke-width: 4;
    outline: none;
  }
  .connection:has(.edge-hit:hover) .edge,
  .connection:focus-within .edge,
  .connection.chosen .edge {
    stroke-width: 3;
    opacity: 1;
  }
  .graph-node {
    position: absolute;
    width: 25%;
    box-sizing: border-box;
    padding: 6px 10px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    text-align: left;
    background: var(--sheet);
    color: var(--ink);
    border: 1px solid var(--node-tone);
    cursor: pointer;
    animation: arrive 0.3s ease-out;
  }
  .graph-node:hover,
  .graph-node.chosen {
    background: color-mix(in srgb, var(--node-tone) 12%, var(--sheet));
    border-width: 2px;
  }
  .role {
    font: 0.6rem var(--font-code);
    color: var(--node-tone);
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }
  .graph-node strong {
    font-size: 0.8rem;
    line-height: 1.2;
    font-weight: 500;
  }
  button:focus-visible {
    outline: 2px solid var(--tone-hot);
    outline-offset: 3px;
  }
  .graph-detail {
    min-height: 3.5rem;
    border-top: 1px solid var(--line);
    padding: 0.75rem 0;
  }
  .detail-top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
  }
  .detail-top strong {
    font-size: 0.8rem;
    line-height: 1.2;
    font-weight: 500;
  }
  .detail-top a {
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  .graph-detail p {
    margin: 0.35rem 0 0;
    font-size: 0.85rem;
  }
  .graph-detail .takeaway {
    font-size: 1rem;
    line-height: 1.5;
    margin: 0 0 0.35rem;
  }
  .summary-source {
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  .waiting {
    color: var(--ink-2);
  }
  details {
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  summary {
    cursor: pointer;
  }
  pre {
    white-space: pre-wrap;
    max-height: 15rem;
    overflow: auto;
  }
  @keyframes arrive {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
  @keyframes connect {
    from {
      opacity: 0;
    }
    to {
      opacity: 0.7;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .edge,
    .graph-node {
      animation: none;
    }
  }
  @media (max-width: 600px) {
    .detail-top {
      align-items: flex-start;
      flex-direction: column;
      gap: 0.3rem;
    }
  }
</style>
