<script>
  let { mode = "memory", title, notes = [] } = $props();
  let selected = $state("hot");
  let step = $state(0);
  let moving = $state(false);
  let cacheMiss = $state(false);
  const phases = {
    prefill: ["Before the forward", "Read selected rows", "Forward"],
    decode: ["Route", "Sort by residency", "Move", "Compute and tally"],
    swap: ["Tick", "Stage", "Flip"],
  };
  const routes = [
    {
      key: "hot",
      label: "GPU resident",
      path: "M120 80 V116 H330",
      nodes: ["gpu"],
      tone: "gpu",
    },
    {
      key: "warm",
      label: "Pinned RAM",
      path: "M120 80 V216 H190 M295 184 V146 H330",
      nodes: ["ram", "gpu"],
      tone: "ram",
    },
    {
      key: "cold",
      label: "Page cache / CPU",
      path: "M120 80 V298 H430 M640 298 H655 V248 M655 184 V156 H540",
      nodes: ["cache", "cpu", "gpu"],
      tone: "cache",
    },
  ];
  const nodes = [
    {
      key: "gpu",
      label: "RTX 4090",
      detail: "24 GB VRAM",
      x: 330,
      y: 90,
      w: 210,
      h: 80,
    },
    {
      key: "ram",
      label: "Pinned RAM",
      detail: "Expert weights",
      x: 190,
      y: 184,
      w: 210,
      h: 64,
    },
    {
      key: "cpu",
      label: "CPU executor",
      detail: "Returns activations",
      x: 550,
      y: 184,
      w: 210,
      h: 64,
    },
    {
      key: "cache",
      label: "Page cache",
      detail: "File pages in RAM",
      x: 430,
      y: 266,
      w: 210,
      h: 64,
    },
    {
      key: "disk",
      label: "NVMe",
      detail: "Expert banks + PLE",
      x: 190,
      y: 266,
      w: 210,
      h: 64,
    },
  ];
  let activeNodes = $derived(
    mode === "memory"
      ? [
          ...routes.find((route) => route.key === selected).nodes,
          ...(selected === "cold" && cacheMiss ? ["disk"] : []),
        ]
      : mode === "swap"
        ? step === 0
          ? ["gpu"]
          : ["disk", "ram", "gpu"]
        : mode === "decode"
          ? step === 0
            ? []
            : step === 1
              ? ["gpu", "ram", "cache", "disk"]
              : ["gpu", "ram", "cpu", "cache"]
          : step === 0
            ? []
            : step === 1
              ? ["disk", "cache", "ram", "gpu"]
              : ["gpu", "ram", "cpu", "cache"],
  );
  let activeRoutes = $derived(
    mode === "memory"
      ? [selected]
      : mode === "swap"
        ? step === 1
          ? ["warm"]
          : []
        : mode === "decode"
          ? step < 2
            ? []
            : ["hot", "warm", "cold"]
          : step === 0
            ? []
            : step === 1
              ? ["staging"]
              : ["hot", "warm", "cold"],
  );
  let visibleRoutes = $derived(
    mode === "swap"
      ? [{ key: "warm", path: "M295 266 V216 H435 V170", tone: "ram" }]
      : mode === "prefill" && step === 1
        ? [
            {
              key: "staging",
              path: "M295 266 V248 M295 184 V146 H330",
              tone: "ram",
            },
          ]
        : routes,
  );
  const memoryNames = [
    "RTX 4090",
    "PCIe",
    "Pinned RAM",
    "Page cache",
    "NVMe",
    "CPU executor",
  ];
  const partNames = {
    A: "GPU",
    B: "Pinned RAM",
    C: "Page cache",
    D: "NVMe",
    E: "CPU executor",
  };
  let currentNote = $derived(
    notes.find((note) => note.key === String(step + 1)),
  );
  function choose(index) {
    step = index;
    moving = false;
  }
  $effect(() => {
    if (!moving || mode === "memory") return;
    const timer = setInterval(() => {
      step = (step + 1) % phases[mode].length;
    }, 2400);
    return () => clearInterval(timer);
  });
</script>

<section class="system-diagram" aria-label={title}>
  <header class="diagram-heading">
    <strong>{title}</strong>
    <button
      type="button"
      aria-pressed={moving}
      onclick={() => (moving = !moving)}
      >{moving ? "Pause flow" : "Animate flow"}</button
    >
  </header>
  {#if mode === "memory"}
    <nav class="diagram-controls" aria-label="Explore expert paths">
      {#each routes as route}
        <button
          type="button"
          class={route.tone}
          aria-pressed={selected === route.key}
          onclick={() => (selected = route.key)}>{route.label}</button
        >
      {/each}
      {#if selected === "cold"}<button
          type="button"
          aria-pressed={cacheMiss}
          onclick={() => (cacheMiss = !cacheMiss)}
          >{cacheMiss ? "Cache miss" : "Cache hit"}</button
        >{/if}
    </nav>
  {:else}
    <nav class="diagram-controls" aria-label="Execution sequence">
      {#each phases[mode] as phase, index}
        <button
          type="button"
          aria-pressed={step === index}
          onclick={() => choose(index)}><span>{index + 1}</span> {phase}</button
        >
      {/each}
    </nav>
  {/if}
  <div class="system-map" class:moving>
    <svg
      viewBox="0 0 800 358"
      role="img"
      aria-label={mode === "memory"
        ? routes.find((route) => route.key === selected).label + " expert path"
        : phases[mode][step]}
    >
      <title>{title}</title>
      <defs>
        {#each ["gpu", "ram", "cache", "disk"] as tone}
          <marker
            id={"system-" + mode + "-" + tone}
            class={tone}
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path class="arrow" d="M0 0 L10 5 L0 10 Z" />
          </marker>
        {/each}
      </defs>
      {#each visibleRoutes as route}
        <g
          class={"flow " + route.tone}
          class:active={activeRoutes.includes(route.key)}
        >
          <path class="track" d={route.path} />
          <path
            class="signal"
            d={route.path}
            marker-end={"url(#system-" + mode + "-" + route.tone + ")"}
          />
        </g>
      {/each}
      {#if mode !== "swap"}
        <g
          class="flow disk"
          class:active={mode === "memory"
            ? selected === "cold" && cacheMiss
            : mode === "prefill" && step === 1}
        >
          <path class="track" d="M400 298 H430" />
          <path
            class="signal"
            d="M400 298 H430"
            marker-end={"url(#system-" + mode + "-disk)"}
          />
        </g>
      {/if}
      {#if mode === "swap"}<path class="advice" d="M120 80 V116 H330" />{/if}
      <path
        class="return"
        class:inactive={mode === "swap"
          ? step !== 2
          : mode !== "memory" && step < (mode === "prefill" ? 2 : 3)}
        d="M540 116 H742 V68"
      />
      <text x="560" y="104" class="edge-label"
        >{mode === "swap" ? "Step boundary" : "combine outputs"}</text
      >
      <text x="292" y="174" class="edge-label">PCIe</text>
      {#if mode !== "swap"}<text x="406" y="340" class="edge-label"
          >NVMe read on cache miss</text
        >{/if}
      <rect class="router" x="34" y="28" width="190" height="52" rx="5" />
      <text x="50" y="50" class="node-title"
        >{mode === "swap"
          ? "Expert usage"
          : mode === "prefill"
            ? "Prompt chunk"
            : "Router"}</text
      >
      <text x="50" y="68" class="node-detail"
        >{mode === "swap"
          ? "Decayed counters"
          : mode === "prefill"
            ? "Known token IDs"
            : "Selected experts"}</text
      >
      <rect class="output" x="610" y="28" width="165" height="40" rx="5" />
      <text x="628" y="53" class="node-title"
        >{mode === "swap" ? "Slot flip" : "Next layer"}</text
      >
      {#each nodes.filter((node) => mode !== "swap" || ["gpu", "ram", "disk"].includes(node.key)) as node}
        <g
          class={"node " + node.key}
          class:active={activeNodes.includes(node.key)}
        >
          <rect x={node.x} y={node.y} width={node.w} height={node.h} rx="5" />
          <text x={node.x + 16} y={node.y + 26} class="node-title"
            >{node.label}</text
          >
          <text x={node.x + 16} y={node.y + 46} class="node-detail"
            >{mode === "prefill" && node.key === "ram" && step === 1
              ? "Selected rows / staging"
              : mode === "swap" && node.key === "gpu"
                ? step === 2
                  ? "Replacement installed"
                  : "Old expert keeps serving"
                : node.detail}</text
          >
          {#if node.key === "gpu"}
            {#each Array(10) as _, index}
              <rect
                class="expert-slot"
                class:replacement={mode === "swap" && step === 2 && index === 9}
                x={node.x + 16 + index * 18}
                y={node.y + 59}
                width="12"
                height="8"
              />
            {/each}
          {/if}
        </g>
      {/each}
    </svg>
  </div>
  <div class="diagram-explanation" aria-live="polite">
    {#if mode === "memory"}
      <p>
        {selected === "hot"
          ? "GPU-resident experts use their local weights."
          : selected === "warm"
            ? "Pinned experts transfer over PCIe into a GPU slot."
            : cacheMiss
              ? "A cache miss reads the selected weights from NVMe. The CPU returns an activation vector to the GPU."
              : "The selected weights are already in the page cache. The CPU returns an activation vector to the GPU."}
      </p>
    {:else if currentNote}
      <p>{@html currentNote.html}</p>
    {/if}
  </div>
  <details class="diagram-notes">
    <summary>Memory and execution details</summary>
    <dl>
      {#each notes as note, index}
        <div>
          <dt>
            {mode === "memory"
              ? memoryNames[index]
              : (partNames[note.key] ?? phases[mode][Number(note.key) - 1])}
          </dt>
          <dd>{@html note.html}</dd>
        </div>
      {/each}
    </dl>
  </details>
  <p class="diagram-footnote">
    Illustrated execution paths. Animation does not represent measured timing.
  </p>
</section>

<style>
  .system-diagram {
    margin: 1.5rem -1rem;
    border-block: 1px solid var(--stroke);
    font-family: var(--font-ui);
    color: var(--ink);
  }
  .diagram-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.8rem 1rem;
  }
  .diagram-heading strong {
    font-size: 0.85rem;
    font-weight: 600;
  }
  button {
    font: 0.72rem var(--font-code);
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.5rem 0.65rem;
    cursor: pointer;
  }
  button:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .diagram-heading button {
    flex-shrink: 0;
  }
  .diagram-controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    padding: 0 1rem 0.8rem;
  }
  .diagram-controls button.gpu {
    border-left: 3px solid var(--tone-gpu);
  }
  .diagram-controls button.ram {
    border-left: 3px solid var(--tone-ram);
  }
  .diagram-controls button.cache {
    border-left: 3px solid var(--tone-cache);
  }
  .diagram-controls button[aria-pressed="true"] {
    background: var(--band);
    border-color: var(--accent-ink);
  }
  .diagram-controls span {
    display: inline-grid;
    place-items: center;
    width: 1.2rem;
    height: 1.2rem;
    background: var(--band);
  }
  .system-map {
    background: var(--band);
    overflow-x: auto;
    border-block: 1px solid var(--line);
  }
  svg {
    display: block;
    width: 100%;
    min-width: 34rem;
    height: auto;
  }
  .gpu {
    --tone: var(--tone-gpu);
  }
  .ram {
    --tone: var(--tone-ram);
  }
  .cache,
  .cpu {
    --tone: var(--tone-cache);
  }
  .disk {
    --tone: var(--tone-disk);
  }
  .node {
    opacity: 1;
    transition: opacity 180ms ease;
  }
  .node.active {
    opacity: 1;
  }
  .node > rect:first-child {
    fill: color-mix(in srgb, var(--tone) 5%, var(--sheet));
    stroke: var(--tone);
    stroke-width: 1.5;
  }
  .node.active > rect:first-child {
    stroke-width: 2.5;
    fill: color-mix(in srgb, var(--tone) 12%, var(--sheet));
  }
  .node-title {
    font: 600 14px var(--font-ui);
    fill: var(--ink);
  }
  .node-detail,
  .edge-label {
    font: 11px var(--font-code);
    fill: var(--ink-2);
  }
  .node .expert-slot.replacement {
    fill: var(--tone-hot);
  }
  .node .expert-slot {
    fill: var(--tone-gpu);
    stroke: none;
  }
  .router,
  .output {
    fill: var(--sheet);
    stroke: var(--stroke);
  }
  .flow {
    opacity: 0.15;
    transition: opacity 180ms ease;
  }
  .flow.active {
    opacity: 1;
  }
  .arrow {
    fill: var(--tone);
  }
  .track,
  .signal {
    fill: none;
    stroke: var(--tone);
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .track {
    stroke-width: 5;
    opacity: 0.18;
  }
  .signal {
    stroke-width: 2;
  }
  /* Motion marks the selected execution path; the static path carries the same meaning. */
  .moving .flow.active .signal {
    stroke-dasharray: 6 18;
    animation: travel 1.4s linear infinite;
  }
  @keyframes travel {
    to {
      stroke-dashoffset: -48;
    }
  }
  .advice {
    fill: none;
    stroke: var(--tone-hot);
    stroke-width: 1.5;
    stroke-dasharray: 4 5;
  }
  .return.inactive {
    opacity: 0.15;
  }
  .return {
    fill: none;
    stroke: var(--tone-gpu);
    stroke-width: 1.5;
  }
  .diagram-explanation {
    min-height: 4.5rem;
    padding: 0.8rem 1rem;
    border-bottom: 1px solid var(--line);
  }
  .diagram-explanation p {
    margin: 0;
    font-size: 0.85rem;
    line-height: 1.6;
  }
  .diagram-notes {
    padding: 0.7rem 1rem;
  }
  summary {
    cursor: pointer;
    font: 0.72rem var(--font-code);
  }
  dl {
    margin-bottom: 0;
  }
  dl > div {
    display: grid;
    grid-template-columns: 8rem minmax(0, 1fr);
    gap: 1rem;
    padding-block: 0.6rem;
    border-top: 1px solid var(--line);
  }
  dt {
    font: 0.72rem var(--font-code);
  }
  dd {
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.6;
  }
  .diagram-footnote {
    margin: 0;
    padding: 0.6rem 1rem;
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  @media (max-width: 600px) {
    .diagram-explanation {
      min-height: 0;
    }
    /* On phones, names and paths carry the overview; the selected explanation carries detail. */
    svg {
      min-width: 0;
    }
    .node-title {
      font-size: 24px;
    }
    .node-detail,
    .edge-label {
      display: none;
    }
    .diagram-heading {
      align-items: flex-start;
    }
    dl > div {
      grid-template-columns: 1fr;
      gap: 0.3rem;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .moving .flow.active .signal {
      animation: none;
      stroke-dasharray: none;
    }
    .node,
    .flow {
      transition: none;
    }
  }
</style>
