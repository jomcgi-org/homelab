<script>
  let { mode = "memory", title, notes = [] } = $props();
  let selected = $state("hot");
  let step = $state(0);
  let cacheMiss = $state(false);
  const phases = {
    prefill: ["Before the forward", "Read selected rows", "Forward"],
    decode: ["Route", "Sort by residency", "Move", "Compute and tally"],
    swap: ["Tick", "Stage", "Flip"],
  };
  const paths = [
    { key: "hot", label: "GPU", tone: "gpu" },
    { key: "warm", label: "Pinned RAM", tone: "ram" },
    { key: "cold", label: "Page cache / CPU", tone: "cache" },
  ];
  const node = (label, detail, tone) => ({ label, detail, tone });
  const gpu = node("GPU", "Resident weights", "gpu");
  const ram = node("Pinned RAM", "Expert weights", "ram");
  const disk = node("NVMe", "Selected rows", "disk");
  const cache = node("Page cache", "Expert weights", "cache");
  const cpu = node("CPU", "Expert compute", "cache");
  const output = node("Combine", "Weighted outputs", "gpu");
  let route = $derived(
    selected === "hot"
      ? [gpu, output]
      : selected === "warm"
        ? [ram, node("GPU", "PCIe transfer", "gpu"), output]
        : [...(cacheMiss ? [disk] : []), cache, cpu, output],
  );
  let flow = $derived(
    mode === "memory"
      ? [node("Router", "Selected experts", "ink"), ...route]
      : mode === "swap"
        ? step === 0
          ? [
              node("Usage", "Decayed counters", "hot"),
              node("Rank", "Hot candidates", "hot"),
            ]
          : step === 1
            ? [
                disk,
                node("Staging", "Old expert still serves", "ram"),
                node("GPU", "Replacement ready", "gpu"),
              ]
            : [
                node("New weights", "Copy complete", "hot"),
                node("Slot mapping", "Flip between steps", "hot"),
                node("GPU", "New expert serves", "gpu"),
              ]
        : mode === "prefill"
          ? step === 0
            ? [
                node("Prompt", "Known token IDs", "ink"),
                node("PLE rows", "Deduplicated lookup IDs", "disk"),
              ]
            : step === 1
              ? [
                  disk,
                  node("Staging", "Selected PLE rows", "ram"),
                  node("GPU", "Compact lookup buffer", "gpu"),
                ]
              : [
                  node("Experts", "Tokens grouped by expert", "ram"),
                  node("CPU / GPU", "Batched compute", "gpu"),
                  output,
                ]
          : step === 0
            ? [
                node("Token", "Current vector", "ink"),
                node("Router", "Select experts", "hot"),
              ]
            : step === 1
              ? [
                  node("Experts", "Selected routes", "hot"),
                  node("Residency", "GPU, RAM or page cache", "ram"),
                ]
              : step === 2
                ? route
                : [
                    node("Experts", "GPU and CPU results", "cache"),
                    output,
                    node("Next layer", "Continue the token", "gpu"),
                  ],
  );
  let currentNote = $derived(
    notes.find((note) => note.key === String(step + 1)),
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
</script>

<section class="system-diagram" aria-label={title}>
  <header><strong>{title}</strong></header>
  {#if mode === "memory" || (mode === "decode" && step === 2)}
    <nav aria-label="Explore expert paths" class="paths">
      {#each paths as path}
        <button
          type="button"
          class={path.tone}
          aria-pressed={selected === path.key}
          onclick={() => (selected = path.key)}>{path.label}</button
        >
      {/each}
      {#if selected === "cold"}<button
          type="button"
          aria-pressed={cacheMiss}
          onclick={() => (cacheMiss = !cacheMiss)}
          >{cacheMiss ? "Cache miss" : "Cache hit"}</button
        >{/if}
    </nav>
  {/if}
  {#if mode !== "memory"}
    <nav aria-label="Execution sequence" class="steps">
      {#each phases[mode] as phase, index}
        <button
          type="button"
          aria-pressed={step === index}
          onclick={() => (step = index)}
          ><span>{index + 1}</span> {phase}</button
        >
      {/each}
    </nav>
  {/if}
  <div
    class="machine"
    class:memory={mode === "memory"}
    class:prefill={mode === "prefill"}
    class:decode={mode === "decode"}
    class:swap={mode === "swap"}
  >
    <svg viewBox="0 0 640 260" role="img" aria-label="Execution path">
      {#if mode === "memory"}
        <g class="ink"
          ><circle cx="54" cy="126" r="27" /><text x="54" y="174">Router</text
          ></g
        >
        {#each paths as path, index}
          <g class={path.tone} class:active={selected === path.key}>
            <path
              class="wire"
              d={`M82 126 C130 126 116 ${44 + index * 86} 166 ${44 + index * 86} H470 C520 ${44 + index * 86} 510 126 554 126`}
            />
            <path
              class="traveller"
              style={`--delay:${index * -0.4}s`}
              d={`M82 126 C130 126 116 ${44 + index * 86} 166 ${44 + index * 86} H470 C520 ${44 + index * 86} 510 126 554 126`}
            />
            <rect x="168" y={22 + index * 86} width="150" height="44" rx="5" />
            <text x="243" y={49 + index * 86}>{path.label}</text>
            <text class="sub" x="402" y={65 + index * 86}
              >{index === 0
                ? "GPU compute"
                : index === 1
                  ? "PCIe → GPU"
                  : cacheMiss
                    ? "NVMe → CPU"
                    : "CPU compute"}</text
            >
          </g>
        {/each}
        <g class="gpu"
          ><circle cx="582" cy="126" r="27" /><text x="582" y="174"
            >Combine</text
          ></g
        >
      {:else if mode === "prefill"}
        <g class:muted={step === 1}>
          <text class="heading" x="92" y="25"
            >{step === 2 ? "Tokens by expert" : "Prompt tokens"}</text
          >
          {#each Array(12) as _, index}
            {@const x = 38 + (index % 3) * 34}
            {@const y = 50 + Math.floor(index / 3) * 40}
            {@const groupedX = 24 + Math.floor(index / 3) * 29}
            {@const groupedY = 58 + (index % 3) * 64}
            <g
              class={index % 3 === 0 ? "gpu" : index % 3 === 1 ? "ram" : "disk"}
            >
              <rect
                class="token"
                style={`transform:translate(${step === 2 ? groupedX - x : 0}px,${step === 2 ? groupedY - y : 0}px)`}
                {x}
                {y}
                width="25"
                height="25"
                rx="3"
              />
              {#if step === 0}
                <path
                  class="wire"
                  d={`M${x + 25} ${y + 12} C205 ${y + 12} 214 ${72 + (index % 3) * 64} 272 ${72 + (index % 3) * 64}`}
                />
                <path
                  class="traveller"
                  style={`--delay:${index * -0.17}s`}
                  d={`M${x + 25} ${y + 12} C205 ${y + 12} 214 ${72 + (index % 3) * 64} 272 ${72 + (index % 3) * 64}`}
                />
              {/if}
            </g>
          {/each}
        </g>
        <text class="heading" x="322" y="25"
          >{step === 2 ? "Expert batches" : "Selected lookup rows"}</text
        >
        <g class:muted={step === 0}>
          <text class="heading" x="554" y="25"
            >{step < 2 ? "GPU buffer" : "CPU / GPU compute"}</text
          >
        </g>
        {#each ["gpu", "ram", "disk"] as tone, index}
          <g class={tone}>
            {#if step === 2}
              <path class="wire" d={`M145 ${72 + index * 64} H272`} />
              <path
                class="traveller"
                style={`--delay:${index * -0.4}s`}
                d={`M145 ${72 + index * 64} H272`}
              />
            {/if}
            <rect x="272" y={50 + index * 64} width="100" height="44" rx="4" />
            {#if step > 0}
              {#each Array(4) as _, row}
                <rect
                  class="row-data"
                  x={step === 2 ? 280 + row * 22 : 280}
                  y={step === 2 ? 58 + index * 64 : 57 + index * 64 + row * 8}
                  width={step === 2 ? 17 : 84}
                  height={step === 2 ? 28 : 4}
                  rx="1"
                />
              {/each}
              <path class="wire" d={`M372 ${72 + index * 64} H500`} />
              <path
                class="traveller"
                style={`--delay:${index * -0.5}s`}
                d={`M372 ${72 + index * 64} H500`}
              />
            {/if}
            <g class:muted={step === 0}>
              {#each Array(step === 2 ? 3 : 5) as _, tile}
                <rect
                  class:batch={step === 2}
                  class:buffer={step === 1}
                  style={`--delay:${tile * -0.13}s`}
                  x={510 + tile * (step === 2 ? 30 : 18)}
                  y={52 + index * 64}
                  width={step === 2 ? 24 : 12}
                  height="40"
                  rx="2"
                />
              {/each}
            </g>
          </g>
        {/each}
      {:else if mode === "decode"}
        <g class="ink" class:muted={step > 0}
          ><circle cx="58" cy="128" r="24" /><text x="58" y="174">Token</text
          ></g
        >
        <g class="hot" class:muted={step > 0}>
          <path class="wire" d="M82 128 H164" />
          {#if step === 0}<path class="traveller" d="M82 128 H164" />{/if}
          <circle cx="194" cy="128" r="30" /><text x="194" y="181">Router</text>
        </g>
        {#each ["gpu", "ram", "cache"] as tone, index}
          {@const y = 42 + index * 86}
          {@const active = step !== 2 || selected === paths[index].key}
          <g class={tone} class:muted={!active}>
            {#if step === 0}
              <path class="wire" d={`M224 128 Q290 ${y} 342 ${y}`} />
              <path
                class="traveller"
                style={`--delay:${index * -0.4}s`}
                d={`M224 128 Q290 ${y} 342 ${y}`}
              />
            {/if}
            {#if step === 1}
              <rect
                class="residency"
                x="314"
                y={y - 30}
                width="108"
                height="60"
                rx="5"
              />
            {/if}
            {#if step === 2 && active}
              <rect x="224" y={y - 18} width="108" height="36" rx="3" />
              <text class="sub" x="278" y={y + 4}
                >{index === 0
                  ? "Resident"
                  : index === 1
                    ? "PCIe fetch"
                    : cacheMiss
                      ? "NVMe read"
                      : "Page cache"}</text
              >
              <path class="wire" d={`M332 ${y} H366`} />
              <path class="traveller" d={`M332 ${y} H366`} />
            {/if}
            <circle
              class:expert={step === 3 || (step === 2 && active)}
              class="expert-node"
              style={`transform:translateX(${step > 1 ? 22 : 0}px)`}
              cx="368"
              cy={y}
              r="24"
            />
            <text class="sub" x={step > 1 ? 390 : 368} y={y + 39}
              >{step === 0
                ? "Selected expert"
                : ["GPU", "Pinned RAM", "CPU"][index]}</text
            >
            {#if step === 3}
              <path class="wire" d={`M414 ${y} Q466 ${y} 516 128`} />
              <path
                class="traveller"
                style={`--delay:${index * -0.4}s`}
                d={`M414 ${y} Q466 ${y} 516 128`}
              />
            {/if}
          </g>
        {/each}
        <g class="gpu" class:muted={step !== 3}>
          <circle cx="542" cy="128" r="26" /><text x="542" y="174">Combine</text
          >
          <path class="wire" d="M568 128 H620" />{#if step === 3}<path
              class="traveller"
              d="M568 128 H620"
            />{/if}
        </g>
      {:else}
        <g class="ram" class:muted={step === 2}>
          <text class="heading" x="105" y="33"
            >{step === 0 ? "Rank candidates" : "Selected weights"}</text
          >
          <rect x="43" y="62" width="124" height="122" rx="5" />
          {#each Array(6) as _, index}
            <rect
              class:rank-bar={step === 0}
              style={`--rank:${index};transform:translateY(${step === 0 ? (5 - 2 * index) * 4 : 0}px)`}
              x="58"
              y={75 + index * 16}
              width={step === 0 ? 94 - index * 12 : 94}
              height="9"
              rx="2"
            />
          {/each}
        </g>
        {#if step === 1}
          <g class="hot"
            ><path class="wire" d="M168 122 H250 Q270 122 280 150" /><path
              class="traveller"
              d="M168 122 H250 Q270 122 280 150"
            /><text class="sub" x="224" y="171">Copy weights</text></g
          >
        {/if}
        <g class="gpu">
          <text class="heading" x="360" y="33">GPU slot</text><rect
            x="280"
            y="62"
            width="160"
            height="122"
            rx="5"
          />
          <g class:muted={step === 2}
            ><rect
              class="incumbent"
              x="293"
              y="75"
              width="134"
              height="43"
              rx="3"
            /><text x="360" y="102"
              >{step === 2 ? "Retired expert" : "Serving expert"}</text
            ></g
          >
          {#if step > 0}<rect
              class:replacement={step === 1}
              x="293"
              y="130"
              width="134"
              height="41"
              rx="3"
            /><text x="360" y="156"
              >{step === 1 ? "Staging weights" : "New expert serves"}</text
            >{/if}
        </g>
        <g class="gpu">
          <path
            class="wire"
            d={step === 2 ? "M440 151 Q496 151 496 95 H556" : "M440 95 H556"}
          />
          <path
            class="traveller"
            d={step === 2 ? "M440 151 Q496 151 496 95 H556" : "M440 95 H556"}
          />
          <circle cx="580" cy="95" r="22" /><text x="568" y="144"
            >Next step</text
          >
        </g>
        {#if step === 2}<g class="hot"
            ><text class="sub" x="420" y="220">Slot mapping switched</text></g
          >{/if}
      {/if}
    </svg>
    <div class="path-description">
      {#each flow as part, index}{#if index}
          →
        {/if}<span class={part.tone}>{part.label}</span>{/each}
    </div>
  </div>
  {#if mode !== "memory" && currentNote}<p
      class="explanation"
      aria-live="polite"
    >
      {@html currentNote.html}
    </p>{/if}
  <details>
    <summary>Details</summary>
    <dl>
      {#each notes as note, index}<div>
          <dt>
            {mode === "memory"
              ? memoryNames[index]
              : (partNames[note.key] ?? phases[mode][Number(note.key) - 1])}
          </dt>
          <dd>{@html note.html}</dd>
        </div>{/each}
    </dl>
  </details>
</section>

<style>
  .system-diagram {
    margin: 1.5rem 0;
    font-family: var(--font-ui);
    color: var(--ink);
  }
  header {
    margin-bottom: 0.8rem;
  }
  header strong {
    font-size: 0.9rem;
    font-weight: 600;
  }
  nav {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-bottom: 1rem;
  }
  button {
    padding: 0.4rem 0.6rem;
    font: 0.72rem var(--font-code);
    color: var(--ink);
    background: transparent;
    border: 0;
    border-bottom: 2px solid var(--line);
    cursor: pointer;
  }
  button[aria-pressed="true"] {
    border-color: var(--tone, var(--accent-ink));
    background: color-mix(
      in srgb,
      var(--tone, var(--accent-ink)) 10%,
      var(--sheet)
    );
  }
  button:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  button span {
    margin-right: 0.3rem;
  }
  .gpu {
    --tone: var(--tone-gpu);
  }
  .ram {
    --tone: var(--tone-ram);
  }
  .cache {
    --tone: var(--tone-cache);
  }
  .disk {
    --tone: var(--tone-disk);
  }
  .hot {
    --tone: var(--tone-hot);
  }
  .ink {
    --tone: var(--ink);
  }
  .machine {
    padding: 1rem 0;
    overflow: hidden;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
  }
  svg :global(g) {
    color: var(--tone);
  }
  svg :global(rect),
  svg :global(circle) {
    fill: color-mix(in srgb, currentColor 12%, var(--sheet));
    stroke: currentColor;
    stroke-width: 1.5;
  }
  svg :global(text) {
    fill: var(--ink);
    font: 12px var(--font-code);
    text-anchor: middle;
  }
  svg :global(.sub) {
    fill: var(--ink-2);
    font-size: 10px;
  }
  svg :global(.heading) {
    font-size: 11px;
  }
  svg :global(.wire),
  svg :global(.traveller),
  svg :global(.flip) {
    fill: none;
    stroke: currentColor;
  }
  svg :global(.wire) {
    stroke-width: 1.5;
    opacity: 0.28;
  }
  svg :global(.traveller) {
    stroke-width: 3;
    stroke-dasharray: 9 30;
    animation: travel 2s linear infinite;
    animation-delay: var(--delay, 0s);
  }
  .memory svg :global(g:not(.active) .traveller) {
    opacity: 0.12;
  }
  .memory svg :global(g.active .traveller) {
    stroke-width: 4;
  }
  svg :global(.expert-node),
  svg :global(.rank-bar) {
    transition:
      transform 0.45s ease,
      width 0.45s ease;
  }
  svg :global(.muted) {
    opacity: 0.15;
    transition: opacity 0.35s ease;
  }
  svg :global(.row-data) {
    fill: currentColor;
    stroke: none;
  }
  svg :global(.buffer) {
    animation: ready 2s ease-in-out infinite;
    animation-delay: var(--delay);
  }
  svg :global(.token) {
    transition: transform 0.55s cubic-bezier(0.2, 0.7, 0.2, 1);
    fill: currentColor;
    opacity: 0.8;
  }
  svg :global(.batch) {
    animation: compute 1.8s ease-in-out infinite;
    animation-delay: var(--delay, 0s);
    transform-box: fill-box;
    transform-origin: center;
  }
  svg :global(.expert) {
    animation: compute 1.8s ease-in-out infinite;
  }
  svg :global(.replacement) {
    animation: ready 4s ease-in-out infinite;
  }
  svg :global(.flip) {
    stroke-dasharray: 6 5;
    stroke-width: 2;
    animation: ready 4s ease-in-out infinite;
  }
  .path-description {
    text-align: center;
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
  }
  .path-description span {
    color: var(--tone);
  }
  @keyframes travel {
    to {
      stroke-dashoffset: -78;
    }
  }
  @keyframes compute {
    0%,
    100% {
      opacity: 0.45;
      transform: scaleY(0.88);
    }
    50% {
      opacity: 1;
      transform: scaleY(1);
    }
  }
  @keyframes ready {
    0%,
    25% {
      opacity: 0.2;
    }
    65%,
    100% {
      opacity: 1;
    }
  }
  .explanation {
    margin: 1rem 0;
    font-size: 0.85rem;
    line-height: 1.6;
  }
  details {
    margin-top: 0.8rem;
  }
  summary {
    cursor: pointer;
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
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
  @media (max-width: 600px) {
    dl > div {
      grid-template-columns: 1fr;
      gap: 0.3rem;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    svg :global(.traveller),
    svg :global(.batch),
    svg :global(.expert),
    svg :global(.replacement),
    svg :global(.flip),
    svg :global(.buffer),
    svg :global(.token) {
      animation: none;
      transition: none;
    }
  }
</style>
