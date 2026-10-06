<script>
  import DataDisplayFixture from "./DataDisplayFixture.svelte";
  const series = [
    { name: "GPU", shape: "circle", length: 88 },
    { name: "Host RAM", shape: "square", length: 72 },
    { name: "Page cache", shape: "triangle", length: 56 },
    { name: "NVMe", shape: "diamond", length: 40 },
    { name: "Hot expert set", shape: "cross", length: 24 },
  ];
  const statuses = [
    { role: "ok", glyph: "✓", label: "OK: sample available" },
    { role: "warn", glyph: "△", label: "Warning: synthetic threshold" },
    { role: "err", glyph: "×", label: "Error: synthetic failure" },
  ];
  let counts = $state({ light: 0, dark: 0, nested: 0 });
</script>

{#snippet samples(id, scheme, nested = false)}
  <section
    data-ds-theme={`technical-drawing-${scheme}`}
    data-sample={id}
    data-contrast-border
    aria-labelledby={`${id}-title`}
  >
    <h2 id={`${id}-title`} data-contrast-text>
      {nested ? "Dark inset in light" : `Technical drawing: ${scheme}`}
    </h2>
    <p data-contrast-text>Primary text: synthetic measurements only.</p>
    <p class="secondary" data-contrast-text>
      Secondary text: no live service or data.
    </p>
    <p class="faint" data-contrast-text>
      Faint text: still meaningful and readable.
    </p>
    <div class="raised" data-contrast-border>
      <p data-contrast-text>Raised surface: primary text.</p>
      <p class="secondary" data-contrast-text>
        Raised surface: secondary text.
      </p>
      <p class="faint" data-contrast-text>Raised surface: faint text.</p>
      <div class="controls">
        <a href={`#${id}-series`} data-contrast-text>Read the series legend</a>
        <button
          onclick={() => counts[id]++}
          data-contrast-text
          data-contrast-border
        >
          Sample action: {counts[id]}
        </button>
      </div>
      <ul class="statuses" aria-label="Synthetic status labels">
        {#each statuses as status}
          <li class={status.role} data-status={status.role}>
            <span aria-hidden="true" data-contrast-text>{status.glyph}</span>
            <span data-contrast-text>{status.label}</span>
          </li>
        {/each}
      </ul>
    </div>
    <h3 id={`${id}-series`} data-contrast-text>Static chart series</h3>
    <ul class="series" aria-label="Labelled chart series and shape key">
      {#each series as item, index}
        <li
          class={`series-${index + 1}`}
          data-series={index + 1}
          data-shape={item.shape}
        >
          <svg viewBox="0 0 20 20" class="marker" aria-hidden="true">
            {#if item.shape === "circle"}
              <circle cx="10" cy="10" r="7" data-contrast-marker />
            {:else if item.shape === "square"}
              <rect x="3" y="3" width="14" height="14" data-contrast-marker />
            {:else if item.shape === "triangle"}
              <path d="M10 2 L18 18 H2 Z" data-contrast-marker />
            {:else if item.shape === "diamond"}
              <path d="M10 1 L19 10 L10 19 L1 10 Z" data-contrast-marker />
            {:else}
              <path
                d="M7 2 H13 V7 H18 V13 H13 V18 H7 V13 H2 V7 H7 Z"
                data-contrast-marker
              />
            {/if}
          </svg>
          <span data-contrast-text>{item.name}: {item.shape}</span>
          <svg viewBox="0 0 100 12" class="bar" aria-hidden="true">
            <rect
              x="0"
              y="2"
              width={item.length}
              height="8"
              data-contrast-marker
            />
          </svg>
        </li>
      {/each}
    </ul>
    <DataDisplayFixture />
    {#if nested}
      <p class="faint" data-contrast-text>
        Explicit dark roles inside the light sheet.
      </p>
    {:else if scheme === "light"}
      {@render samples("nested", "dark", true)}
    {/if}
  </section>
{/snippet}

<main class="fixture">
  {@render samples("light", "light")}
  {@render samples("dark", "dark")}
</main>

<style>
  :global(body) {
    margin: 0;
  }
  .fixture {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  section {
    min-width: 0;
    padding: var(--ds-space-md);
    font-family: var(--ds-font-body);
    border: var(--ds-border-weight) solid var(--ds-line-strong);
    border-radius: var(--ds-radius);
  }
  section section {
    margin-top: var(--ds-space-md);
  }
  h2,
  h3,
  p,
  span,
  a,
  button {
    overflow-wrap: anywhere;
    min-width: 0;
  }
  h2 {
    font-family: var(--ds-font-display);
    font-size: 1.35rem;
    line-height: 1.3;
    margin: 0 0 var(--ds-space-md);
  }
  h3 {
    font-size: 1rem;
    line-height: 1.4;
  }
  p {
    margin: 0 0 var(--ds-space-sm);
    line-height: 1.5;
  }
  .secondary {
    color: var(--ds-ink-muted);
  }
  .faint {
    color: var(--ds-ink-faint);
  }
  .raised {
    background: var(--ds-surface-raised);
    border: var(--ds-border-weight) solid var(--ds-line-strong);
    padding: var(--ds-space-sm);
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-space-md);
  }
  a {
    color: var(--ds-accent-ink);
    line-height: 1.5;
  }
  button {
    font: inherit;
    line-height: 1.5;
    color: var(--ds-on-accent);
    background: var(--ds-accent);
    border: var(--ds-border-weight) solid var(--ds-accent);
    border-radius: var(--ds-radius);
    padding: var(--ds-space-xs);
    max-width: 100%;
    cursor: pointer;
  }
  a:focus-visible,
  button:focus-visible {
    outline: var(--ds-focus-width) solid var(--ds-focus);
    outline-offset: 3px;
  }
  ul {
    padding: 0;
    list-style: none;
  }
  li {
    min-width: 0;
  }
  .statuses li {
    display: flex;
    gap: var(--ds-space-xs);
    line-height: 1.5;
  }
  .ok {
    color: var(--ds-ok);
  }
  .warn {
    color: var(--ds-warn);
  }
  .err {
    color: var(--ds-err);
  }
  .series li {
    display: grid;
    grid-template-columns: 1.25em minmax(0, 1fr);
    gap: var(--ds-space-xs);
    margin-bottom: var(--ds-space-sm);
    align-items: center;
  }
  .series span {
    color: var(--ds-ink);
    line-height: 1.5;
  }
  .marker {
    width: 1.25em;
    height: 1.25em;
  }
  .bar {
    grid-column: 2;
    width: 100%;
    height: 12px;
  }
  .series-1 svg {
    fill: var(--ds-series-1);
  }
  .series-2 svg {
    fill: var(--ds-series-2);
  }
  .series-3 svg {
    fill: var(--ds-series-3);
  }
  .series-4 svg {
    fill: var(--ds-series-4);
  }
  .series-5 svg {
    fill: var(--ds-series-5);
  }
  @media (max-width: 700px) {
    .fixture {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
