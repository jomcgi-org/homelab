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
  <ol class="flow" aria-label="Execution path">
    {#each flow as part, index}
      <li class={part.tone}>
        {#if index > 0}<span class="connector" aria-hidden="true"><i></i></span
          >{/if}
        <div class="part">
          <strong>{part.label}</strong><span>{part.detail}</span>
        </div>
      </li>
    {/each}
  </ol>
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
  .flow {
    display: flex;
    padding: 0;
    margin: 0;
    list-style: none;
  }
  .flow li {
    display: flex;
    align-items: center;
    flex: 1;
    min-width: 0;
  }
  .part {
    width: 100%;
    min-width: 0;
    padding: 1rem 0.7rem 0.7rem;
    background: color-mix(in srgb, var(--tone) 9%, var(--sheet));
    border-top: 3px solid var(--tone);
  }
  .part strong {
    display: block;
    font-size: 0.85rem;
  }
  .part > span {
    display: block;
    min-height: 2.5em;
    margin-top: 0.4rem;
    font: 0.65rem/1.4 var(--font-code);
    color: var(--ink-2);
  }
  .connector {
    position: relative;
    width: 24px;
    flex-shrink: 0;
    height: 2px;
    background: color-mix(in srgb, var(--tone) 25%, var(--sheet));
  }
  .connector::after {
    content: "";
    position: absolute;
    right: 0;
    top: -3px;
    width: 6px;
    height: 6px;
    border-right: 2px solid var(--tone);
    border-top: 2px solid var(--tone);
    transform: rotate(45deg);
  }
  .connector i {
    position: absolute;
    width: 7px;
    height: 3px;
    top: -1px;
    background: var(--tone);
    animation: flow 1.2s linear infinite;
  }
  @keyframes flow {
    from {
      transform: translateX(0);
      opacity: 0;
    }
    20% {
      opacity: 1;
    }
    to {
      transform: translateX(17px);
      opacity: 0;
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
    .flow {
      flex-direction: column;
      gap: 0;
    }
    .flow li {
      flex-direction: column;
      width: 100%;
    }
    .part {
      box-sizing: border-box;
      padding: 0.7rem;
    }
    .part > span {
      min-height: 0;
    }
    .connector {
      transform: rotate(90deg);
      margin-block: 10px;
    }
    dl > div {
      grid-template-columns: 1fr;
      gap: 0.3rem;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .connector i {
      animation: none;
    }
  }
</style>
