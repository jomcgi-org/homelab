<script>
  /**
   * Labelled cluster of parts, drawn as one outline partitioned by lines
   * that run edge to edge: a title band, then the parts as cells divided by
   * rules, never a box drawn inside a box. `stack` lays the cells
   * vertically (default horizontal).
   * @type {{ label: string, stack?: boolean, children: import('svelte').Snippet }}
   */
  let { label, stack = false, children } = $props();
</script>

<div class="dgroup" class:stack>
  <span class="dgroup-label">{label}</span>
  <div class="dgroup-body" class:stack>
    {@render children()}
  </div>
</div>

<style>
  .dgroup {
    display: flex;
    flex: none;
    flex-direction: column;
    border: 1px solid var(--ink);
    background: var(--sheet);
  }

  .dgroup-label {
    padding: 0.3em 0.7em;
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.62rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .dgroup-body {
    display: flex;
    align-items: stretch;
  }

  .dgroup-body.stack {
    flex-direction: column;
  }

  /* The group is the outline; its parts become cells. */
  .dgroup-body > :global(.dbox) {
    flex: 1 1 auto;
    border: 0;
  }

  .dgroup-body > :global(.dbox + .dbox) {
    border-left: 1px solid var(--stroke);
  }

  .dgroup-body.stack > :global(.dbox + .dbox) {
    border-top: 1px solid var(--stroke);
    border-left: 0;
  }

  @media (max-width: 720px) {
    .dgroup {
      width: 100%;
    }

    .dgroup-body {
      flex-direction: column;
    }

    .dgroup-body > :global(.dbox + .dbox) {
      border-top: 1px solid var(--stroke);
      border-left: 0;
    }
  }
</style>
