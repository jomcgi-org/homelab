<script>
  /**
   * Outer frame for an architecture diagram: bordered panel with an
   * eyebrow label, containing a flow of DBox/DGroup/DArrow children.
   * Flows horizontally on desktop, stacks vertically under 720px.
   * @type {{ label?: string, children: import('svelte').Snippet }}
   */
  let { label = "Architecture", children } = $props();
</script>

<figure class="diagram" role="img" aria-label={label}>
  <figcaption class="diagram-label mono">{label}</figcaption>
  <div class="diagram-flow">
    {@render children()}
  </div>
</figure>

<style>
  .diagram {
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    background: var(--paper);
    padding: 20px;
    margin: 0;
  }

  .diagram-label {
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 16px;
  }

  .diagram-flow {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 10px;
    flex-wrap: wrap;
  }

  @media (max-width: 720px) {
    .diagram-flow {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
