<script>
  let { mode, onChange } = $props();
  // mode: 'pending' | 'audit'
  // onChange: (mode: 'pending' | 'audit') => void
</script>

<div class="toggle" role="tablist" aria-label="Review mode">
  <button
    role="tab"
    aria-selected={mode === "pending"}
    class:active={mode === "pending"}
    onclick={() => onChange("pending")}
  >
    Pending
  </button>
  <button
    role="tab"
    aria-selected={mode === "audit"}
    class:active={mode === "audit"}
    onclick={() => onChange("audit")}
  >
    Audit auto-decisions
  </button>
</div>

<style>
  .toggle {
    display: inline-flex;
    gap: 0.25rem;
  }

  .toggle button {
    font-family: var(--font);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    background: transparent;
    border: none;
    padding: 0.4rem 0.6rem;
    cursor: pointer;
  }

  .toggle button.active {
    color: var(--fg);
  }

  .toggle button:focus-visible {
    outline: 1.5px solid var(--fg);
    outline-offset: 2px;
  }
</style>
