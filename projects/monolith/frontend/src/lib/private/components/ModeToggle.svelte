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
  /* Brutalist tablist: explicit mono + hover/active underline that
     mirrors Nav.svelte's coral underline pattern, so this toggle reads
     as a sibling of the site nav rather than a custom widget. */

  .toggle {
    display: inline-flex;
    gap: 0.25rem;
  }

  .toggle button {
    position: relative;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    background: transparent;
    border: none;
    padding: 0.5rem 0.75rem;
    cursor: pointer;
  }

  .toggle button::after {
    content: "";
    position: absolute;
    left: 0.75rem;
    right: 0.75rem;
    bottom: 0.2rem;
    height: 2px;
    background: var(--coral);
    transform: scaleX(0);
    transition: transform 160ms ease;
  }

  .toggle button:hover::after {
    transform: scaleX(1);
  }

  .toggle button.active {
    color: var(--fg);
  }

  .toggle button.active::after {
    background: var(--fg);
    transform: scaleX(1);
  }

  .toggle button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
</style>
