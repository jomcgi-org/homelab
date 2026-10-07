<script>
  import DemoDisclosure from "./DemoDisclosure.svelte";
  import { incidentGraph, layoutIncidentGraph } from "./incident-graph.js";
  let {
    answer = "",
    finalAnswer = "",
    complete = false,
    sourceUrl = "",
    review = {},
    outputOpen = $bindable(true),
    landing = false,
  } = $props();
  let output = $state();
  let followOutput = $state(true);
  let previousLength = 0;
  $effect(() => {
    const length = answer.length;
    const expanded = outputOpen;
    if (length < previousLength) followOutput = true;
    previousLength = length;
    if (output && followOutput && expanded)
      output.scrollTop = output.scrollHeight;
  });
  function trackOutputScroll() {
    followOutput =
      output.scrollHeight - output.clientHeight - output.scrollTop < 24;
  }
  const instance = `incident-${Math.random().toString(36).slice(2)}`;
  let captured = $derived(incidentGraph(answer, complete));
  // Keep the captured response intact; use verified page links and relationship types in the view.
  let graph = $derived({
    summary: captured.summary,
    nodes: captured.nodes.map((item) => ({ ...item, ...review[item.id] })),
    edges: captured.edges.map((item) => ({ ...item, ...review[item.id] })),
  });
  let layout = $derived(layoutIncidentGraph(incidentGraph(finalAnswer, true)));
  let detailStatements = $derived(
    (() => {
      const final = incidentGraph(finalAnswer, true);
      return [final.summary, ...final.nodes, ...final.edges]
        .filter(Boolean)
        .map((item) => ({ ...item, ...review[item.id] }));
    })(),
  );
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
            // Long edges cross in the band between the lane labels and the
            // first row of nodes (see layoutIncidentGraph), not on the labels.
            [x1 + direction * 24, 66 - (index % 3) * 6],
            [x2 - direction * 24, 66 - (index % 3) * 6],
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

<div class="incident-graph" class:landing>
  <div class="graph-scroll">
    <div
      class="graph-canvas"
      style={`aspect-ratio:840 / ${layout.height};--graph-height:${layout.height}`}
    >
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
              markerUnits="userSpaceOnUse"
              markerWidth="10"
              markerHeight="10"
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
            class:chosen={active?.id === node.id ||
              active?.from === node.id ||
              active?.to === node.id}
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
  <div class="inspector">
    <div class="graph-detail" aria-live="polite">
      <div class="detail-copy">
        <p class:takeaway={active?.type === "summary"}>
          {active?.detail ?? "Building the control graph…"}
        </p>
        <div class="detail-reserve" aria-hidden="true">
          {#each detailStatements as item (item.id)}<p>{item.detail}</p>{/each}
        </div>
      </div>
      <div class="detail-source">
        {#if active}<a
            href={`${sourceUrl}#page=${active.pages[0]}`}
            target="_blank"
            rel="noreferrer"
            >{active.basis === "inferred" ? "Analysis · " : ""}Report p. {active.pages.join(
              ", ",
            )}</a
          >{/if}
      </div>
    </div>

    <DemoDisclosure
      class="model-output"
      label="Model output"
      bind:open={outputOpen}
    >
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users can scroll the output.) -->
      <pre
        bind:this={output}
        onscroll={trackOutputScroll}
        tabindex="0"
        role="region"
        aria-label="Streaming model output"><code
          >{answer}{#if !complete}<span class="stream-cursor" aria-hidden="true"
            ></span>{/if}</code
        ></pre>
    </DemoDisclosure>
  </div>
</div>

<style>
  .incident-graph {
    min-width: 0;
  }
  .inspector {
    min-width: 0;
  }
  @media (min-width: 901px) {
    .landing {
      display: grid;
      grid-template-columns: minmax(0, 1.8fr) minmax(16rem, 1fr);
      gap: 1.5rem;
      align-items: start;
    }
    /* Fit the landing in one screen: the graph narrows (and so shortens)
       until the page chrome above, the draft trace and the link below fit. */
    .landing .graph-canvas {
      min-width: 500px;
      max-width: min(840px, calc((100svh - 35rem) * 840 / var(--graph-height)));
    }
    .landing .graph-detail {
      border-top: 0;
      padding-top: 1rem;
    }
    .landing pre {
      height: 10rem;
    }
  }
  @media (max-width: 900px) {
    .landing .graph-canvas {
      min-width: 560px;
    }
    .landing .boundaries span {
      padding: 12px 16px;
      font-size: 0.6rem;
    }
    .landing .role {
      display: none;
    }
    .landing pre {
      height: 3.5rem;
    }
    .landing .graph-detail {
      padding-block: 0.5rem;
    }
    .landing .graph-detail p {
      font-size: 0.85rem;
      line-height: 1.4;
    }
  }
  @container (max-width: 640px) {
    .landing .role {
      display: none;
    }
    .landing .boundaries span {
      padding: 12px 16px;
      font-size: 0.6rem;
    }
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
    container-type: inline-size;
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
    opacity: 0.6;
    transition: opacity 150ms ease;
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
    outline: none;
  }
  .connection:has(.edge-hit:focus-visible) .edge {
    stroke: var(--ink);
  }
  .connection:has(.edge-hit:hover) .edge,
  .connection:focus-within .edge,
  .connection.chosen .edge {
    opacity: 1;
  }
  .graph-node {
    position: absolute;
    width: 25%;
    box-sizing: border-box;
    padding: 3px 8px;
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: clamp(0.75rem, 1.85cqw, 0.8rem);
    text-align: left;
    background: var(--sheet);
    color: var(--ink);
    border: 1px solid var(--node-tone);
    cursor: pointer;
    transition: background-color 150ms ease;
    animation: arrive 0.3s ease-out;
  }
  .graph-node:hover,
  .graph-node.chosen {
    background: color-mix(in srgb, var(--node-tone) 5%, var(--sheet));
  }
  .role {
    font: 0.7em/1.1 var(--font-code);
    color: var(--ink-2);
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }
  .graph-node strong {
    font-size: inherit;
    line-height: 1.2;
    font-weight: 500;
  }
  button:focus-visible {
    outline: 2px solid var(--tone-hot);
    outline-offset: 3px;
  }
  .graph-detail {
    border-top: 1px solid var(--line);
    padding: 0.75rem 0;
  }
  .detail-copy,
  .detail-reserve {
    display: grid;
  }
  .detail-copy > p,
  .detail-reserve,
  .detail-reserve p {
    grid-area: 1 / 1;
  }
  .detail-reserve {
    visibility: hidden;
    pointer-events: none;
  }
  .graph-detail p {
    margin: 0;
    font-size: 0.95rem;
    line-height: 1.5;
  }
  .detail-source {
    min-height: 1rem;
    margin-top: 0.35rem;
  }
  .detail-source a {
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  pre {
    height: 5rem;
    box-sizing: border-box;
    margin: 0.5rem 0 0;
    padding: 0.5rem;
    border: 1px solid var(--line);
    background: var(--band);
    color: var(--ink-2);
    font: 0.7rem/1.5 var(--font-code);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    overflow: auto;
    scrollbar-width: thin;
  }
  pre:focus-visible {
    outline: 2px solid var(--tone-gpu);
    outline-offset: 2px;
  }
  code {
    font: inherit;
  }
  .stream-cursor {
    display: inline-block;
    height: 1em;
    margin-left: 2px;
    border-left: 2px solid var(--tone-gpu);
    vertical-align: -0.1em;
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
  }
  @media (prefers-reduced-motion: reduce) {
    .edge,
    .graph-node {
      transition: none;
    }
    .edge,
    .graph-node {
      animation: none;
    }
  }
</style>
