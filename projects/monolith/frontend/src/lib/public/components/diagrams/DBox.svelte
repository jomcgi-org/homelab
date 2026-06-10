<script>
  /**
   * Diagram node: hard-shadowed ink-bordered chip. Boxes are neutral
   * (paper) by default so colour never has to be decoded; the one
   * exception is `role="external"`, which gets a single tint to mark
   * genuine third-party systems (HuggingFace, Cloudflare, AISStream).
   * That "yours vs the outside world" split is the only distinction
   * that holds across every diagram, so it is the only one we colour.
   * The other role names are kept for authoring intent and a11y, but
   * render identically.
   * @type {{ role?: 'source'|'process'|'store'|'output'|'external', sub?: string, children: import('svelte').Snippet }}
   */
  let { role = "process", sub = "", children } = $props();

  const bg = role === "external" ? "var(--diagram-external)" : "var(--paper)";
</script>

<span class="dbox mono" style:background={bg}>
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
