<script>
  /**
   * Diagram part: one outlined cell with a mono label and an optional
   * sub-label. Parts are neutral so colour never has to be decoded; the one
   * exception is `role="external"`, drawn with a dashed outline to mark a
   * genuine third-party system (HuggingFace, Cloudflare, AISStream). That
   * "yours vs the outside world" split is the only distinction that holds
   * across every diagram, so it is the only one drawn. The other role names
   * are kept for authoring intent and render identically.
   * @type {{ role?: 'source'|'process'|'store'|'output'|'external', sub?: string, children: import('svelte').Snippet }}
   */
  let { role = "process", sub = "", children } = $props();
</script>

<span class="dbox" class:external={role === "external"}>
  {@render children()}
  {#if sub}<span class="dbox-sub">{sub}</span>{/if}
</span>

<style>
  .dbox {
    display: inline-flex;
    flex: none;
    flex-direction: column;
    gap: 2px;
    align-items: center;
    justify-content: center;
    padding: 0.5em 0.8em;
    border: 1px solid var(--ink);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.72rem;
    font-weight: 600;
    line-height: 1.3;
    text-align: center;
  }

  .dbox.external {
    border-style: dashed;
  }

  .dbox-sub {
    color: var(--ink-2);
    font-size: 0.64rem;
    font-weight: 400;
  }
</style>
