<script>
  // Frame shared by the post's interactive figures: the same caption row,
  // rules and figure counter as the static ones, then controls and a note.
  // Figures use div and span only: the post's p, ul, li and table rules
  // outrank component-scoped styles.
  let { title, controls, children, note } = $props();
</script>

<figure class="fig ix">
  <figcaption>{title}</figcaption>
  {#if controls}<div class="ix-controls">{@render controls()}</div>{/if}
  <div class="ix-body">{@render children()}</div>
  {#if note}<div class="ix-note">{@render note()}</div>{/if}
</figure>

<style>
  .ix-controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    align-items: center;
    padding: 0.75rem 1rem 0;
  }
  .ix-body {
    padding: 0.75rem 1rem 1rem;
    min-width: 0;
  }
  .ix-note {
    margin: 0;
    padding: 0.55em 1rem;
    border-top: 1px solid var(--line);
    color: var(--ink-2);
    font: 0.68rem / 1.5 var(--font-code);
  }
  /* Segmented buttons, as in the demo switch. Shared through :global so each
     figure's controls look the same without repeating the rules. */
  .ix-controls :global(.seg) {
    display: flex;
    flex-wrap: wrap;
    border: 1px solid var(--ink);
  }
  .ix-controls :global(.seg button) {
    min-height: 2.5rem;
    padding: 0.35rem 0.75rem;
    border: 0;
    background: var(--sheet);
    color: var(--ink);
    font: 0.72rem var(--font-code);
    cursor: pointer;
  }
  .ix-controls :global(.seg button + button) {
    border-left: 1px solid var(--ink);
  }
  .ix-controls :global(.seg button[aria-pressed="true"]) {
    background: var(--ink);
    color: var(--sheet);
  }
  .ix-controls :global(button:focus-visible),
  .ix-controls :global(input:focus-visible) {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
  .ix-controls :global(.ix-label) {
    color: var(--ink-2);
    font: 0.68rem var(--font-code);
  }
  .ix-controls :global(input[type="range"]) {
    flex: 1 1 12rem;
    min-width: 0;
    min-height: 2.5rem;
    accent-color: var(--tone-gpu);
  }
  @media (max-width: 600px) {
    .ix-controls :global(.seg) {
      width: 100%;
    }
    .ix-controls :global(.seg button) {
      flex: 1 1 auto;
    }
    .ix-controls :global(.seg button + button) {
      border-left: 1px solid var(--ink);
    }
  }
</style>
