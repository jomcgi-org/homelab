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
    nodes: captured.nodes.map((item) => ({ ...item, ...review[item.id] })),
    edges: captured.edges.map((item) => ({ ...item, ...review[item.id] })),
  });
  let layout = $derived(layoutIncidentGraph(incidentGraph(finalAnswer, true)));
  let canvasElement;
  let scale = $state(1);
  $effect(() => {
    if (!canvasElement || typeof ResizeObserver === "undefined") return;
    const resize = new ResizeObserver(
      ([entry]) =>
        (scale = Math.min(1, Math.max(0.76, entry.contentRect.width / 840))),
    );
    resize.observe(canvasElement);
    return () => resize.disconnect();
  });
  let selected = $state(null);
  let active = $derived(
    [...graph.nodes, ...graph.edges].find((item) => item.id === selected) ??
      graph.edges.at(-1) ??
      graph.nodes.at(-1),
  );
  function path(edge, index) {
    const from = layout.nodes.find((n) => n.id === edge.from);
    const to = layout.nodes.find((n) => n.id === edge.to);
    if (!from || !to) return "";
    if (from.x === to.x) {
      const x = from.x + 105;
      const down = to.y > from.y;
      return `M${x},${from.y + (down ? 62 : 0)} C${x + 50},${from.y + 100} ${x + 50},${to.y - 40} ${x},${to.y + (down ? 0 : 62)}`;
    }
    const right = to.x > from.x;
    const x1 = from.x + (right ? 210 : 0),
      x2 = to.x + (right ? 0 : 210);
    const y1 = from.y + 25 + (index % 3) * 6,
      y2 = to.y + 25 + (index % 3) * 6;
    if (Math.abs(to.x - from.x) > 280) {
      const lane = 43 - (index % 3) * 5;
      return `M${x1},${y1} C${x1 + 35},${y1} ${x1 + 35},${lane} ${x1 + 60},${lane} L${x2 - 50},${lane} C${x2 - 20},${lane} ${x2 - 20},${y2} ${x2},${y2}`;
    }
    const bend = Math.max(40, Math.abs(x2 - x1) / 2);
    return `M${x1},${y1} C${x1 + (right ? bend : -bend)},${y1} ${x2 + (right ? -bend : bend)},${y2} ${x2},${y2}`;
  }
  const tone = (item) =>
    item.kind === "failure"
      ? "disk"
      : item.kind === "feedback" || item.role === "monitor"
        ? "ram"
        : "gpu";
  const choose = (item) => (selected = item.id);
</script>

<div class="incident-graph">
  <div class="graph-scroll" bind:this={canvasElement}>
    <div class="graph-canvas" style={`height:${layout.height}px;zoom:${scale}`}>
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
        {#each graph.edges as edge, i}
          <g
            class="connection"
            class:chosen={active?.id === edge.id}
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
            <circle r="3" fill={`var(--tone-${tone(edge)})`} class="flow-dot"
              ><animateMotion
                dur={`${2.4 + i * 0.15}s`}
                repeatCount="indefinite"
                path={path(edge, i)}
              /></circle
            >
          </g>
        {/each}
      </svg>
      {#each layout.nodes as node}
        {#if graph.nodes.some((n) => n.id === node.id)}
          <button
            class="graph-node"
            class:chosen={active?.id === node.id}
            style={`left:${node.x}px;top:${node.y}px;--node-tone:var(--tone-${tone(node)})`}
            onclick={() => choose(node)}
            aria-pressed={active?.id === node.id}
          >
            <span class="role">{node.role}</span><strong>{node.label}</strong>
          </button>
        {/if}
      {/each}
    </div>
  </div>
  <div class="graph-detail" aria-live="polite">
    {#if active}<div class="detail-top">
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
    width: 840px;
    transform-origin: top left;
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
    animation: arrive 0.6s ease;
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
  .connection.chosen .edge {
    stroke-width: 3;
    opacity: 1;
  }
  .graph-node {
    position: absolute;
    width: 210px;
    min-height: 62px;
    padding: 9px 12px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    text-align: left;
    background: var(--sheet);
    color: var(--ink);
    border: 1px solid var(--node-tone);
    cursor: pointer;
    animation: arrive 0.4s ease;
  }
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
    font-size: 0.85rem;
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
    font-size: 0.85rem;
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
    }
    to {
      opacity: 1;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .edge,
    .graph-node {
      animation: none;
    }
    .flow-dot {
      display: none;
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
