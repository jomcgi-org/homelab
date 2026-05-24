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
  /* Brutalist tablist: chunky stamp blocks instead of subtle underlines.
     Active = filled cream block with 2px border and offset shadow (looks
     like a sticker). Hover = inactive button gets an outline preview of
     the same frame so the click target advertises itself. */

  .toggle {
    display: inline-flex;
    gap: 0.6rem;
    align-items: center;
  }

  .toggle button {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    background: transparent;
    border: 2px solid transparent;
    padding: 0.4rem 0.65rem;
    cursor: pointer;
    transform: translate(0, 0);
    transition:
      transform 0.08s ease,
      box-shadow 0.08s ease,
      background 0.08s ease,
      color 0.08s ease,
      border-color 0.08s ease;
  }

  .toggle button:hover:not(.active) {
    color: var(--fg);
    border-color: var(--fg);
    transform: translate(-2px, -2px);
    box-shadow: 4px 4px 0 0 var(--fg);
  }

  .toggle button.active {
    color: var(--fg);
    background: var(--cream);
    border-color: var(--fg);
  }

  .toggle button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
</style>
