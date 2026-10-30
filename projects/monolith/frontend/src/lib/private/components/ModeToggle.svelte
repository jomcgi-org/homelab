<script>
  let { mode, onChange } = $props();
  // mode: 'pending' | 'audit'
  // onChange: (mode: 'pending' | 'audit') => void
</script>

<div class="toggle" role="tablist" aria-label="Review mode">
  <button
    role="tab"
    aria-selected={mode === "pending"}
    class:here={mode === "pending"}
    onclick={() => onChange("pending")}
  >
    Pending
  </button>
  <button
    role="tab"
    aria-selected={mode === "audit"}
    class:here={mode === "audit"}
    onclick={() => onChange("audit")}
  >
    Audit auto-decisions
  </button>
</div>

<style>
  /* The mode strip is the second divided tab strip in the review band. */
  .toggle {
    display: inline-flex;
    margin: -0.55em 0;
  }

  .toggle button {
    padding: 0.55em 0.8em;
    border: 0;
    border-bottom: 2px solid transparent;
    background: none;
    color: var(--ink-2);
    font: inherit;
    cursor: pointer;
  }

  .toggle button + button {
    border-left: 1px solid var(--stroke);
  }

  .toggle button:hover {
    color: var(--ink);
  }

  .toggle button.here {
    border-bottom-color: var(--accent-ink);
    color: var(--accent-ink);
  }
</style>
