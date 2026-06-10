<script>
  /**
   * Labeled cluster of nodes, the subgraph motif: dashed border, mono
   * label tab. `stack` lays children vertically (default horizontal).
   * @type {{ label: string, stack?: boolean, children: import('svelte').Snippet }}
   */
  let { label, stack = false, children } = $props();
</script>

<div class="dgroup" class:stack>
  <span class="dgroup-label mono">{label}</span>
  <div class="dgroup-body" class:stack>
    {@render children()}
  </div>
</div>

<style>
  .dgroup {
    border: 2px dashed var(--rule-2);
    border-radius: var(--radius);
    padding: 18px 12px 12px;
    position: relative;
    flex-shrink: 0;
  }

  .dgroup-label {
    position: absolute;
    top: -9px;
    left: 10px;
    background: var(--paper);
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .dgroup-body {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .dgroup-body.stack {
    flex-direction: column;
    align-items: stretch;
  }

  @media (max-width: 720px) {
    .dgroup {
      width: 100%;
    }
    .dgroup-body {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
