<script>
  /**
   * Diagram node: hard-shadowed ink-bordered chip. `role` picks the fill
   * from a fixed palette so diagrams stay consistent across projects:
   * source (coral), process (yellow), store (blue), output (green),
   * external (paper).
   * @type {{ role?: 'source'|'process'|'store'|'output'|'external', sub?: string, children: import('svelte').Snippet }}
   */
  let { role = "process", sub = "", children } = $props();

  const fills = {
    source: "var(--coral)",
    process: "var(--accent)",
    store: "var(--blue)",
    output: "var(--green)",
    external: "var(--paper)",
  };
</script>

<span class="dbox mono" style:background={fills[role]}>
  {@render children()}
  {#if sub}<span class="dbox-sub">{sub}</span>{/if}
</span>

<style>
  .dbox {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: 2px;
    border: 2px solid var(--ink);
    border-radius: 6px;
    box-shadow: var(--shadow-hard-sm);
    padding: 8px 12px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink);
    flex-shrink: 0;
  }

  .dbox-sub {
    font-size: 10px;
    font-weight: 400;
    color: var(--ink-2);
    letter-spacing: 0.02em;
  }
</style>
