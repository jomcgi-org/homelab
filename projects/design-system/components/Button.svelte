<script>
  let {
    type = "button",
    disabled = false,
    variant = "primary",
    children,
    class: className = "",
    ...rest
  } = $props();
</script>

<button {...rest} {type} {disabled} class={className} data-variant={variant}>
  {#if children}{@render children()}{/if}
</button>

<style>
  button {
    box-sizing: border-box;
    min-width: 44px;
    min-height: 44px;
    max-width: 100%;
    padding: var(--ds-space-xs) var(--ds-space-sm);
    border: var(--ds-border-weight) solid var(--ds-ink);
    border-radius: var(--ds-radius);
    background: var(--ds-accent);
    color: var(--ds-on-accent);
    box-shadow: var(--ds-shadow);
    font: inherit;
    font-weight: 600;
    overflow-wrap: anywhere;
    cursor: pointer;
  }
  button[data-variant="secondary"],
  button[data-variant="quiet"] {
    background: var(--ds-surface-raised);
    color: var(--ds-ink);
  }
  button[data-variant="quiet"] {
    background: var(--ds-surface);
    border-color: transparent;
    box-shadow: none;
    text-decoration: underline;
    text-underline-offset: 0.2em;
  }
  button:focus-visible {
    outline: var(--ds-focus-width, var(--ds-border-weight)) solid
      var(--ds-focus, var(--ds-ink));
    outline-offset: 3px;
  }
  button:disabled {
    background: var(--ds-surface);
    color: var(--ds-ink-muted);
    border: var(--ds-border-weight) dashed var(--ds-ink-muted);
    box-shadow: none;
    cursor: not-allowed;
  }
  @media (prefers-reduced-motion: reduce) {
    button {
      transition: none;
      animation: none;
    }
  }
</style>
