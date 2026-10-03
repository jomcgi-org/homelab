<script>
  let {
    label,
    open = $bindable(false),
    class: className = "",
    children,
  } = $props();
  const panelId = $props.id();
</script>

<div class={className}>
  <button
    type="button"
    class="disclosure-toggle"
    aria-expanded={open}
    aria-controls={panelId}
    onclick={() => (open = !open)}
  >
    <svg
      class:expanded={open}
      width="10"
      height="10"
      viewBox="0 0 10 10"
      aria-hidden="true"><path d="M3 2 L6 5 L3 8" /></svg
    >
    {label}
  </button>
  <div
    id={panelId}
    class="disclosure-panel"
    class:expanded={open}
    inert={!open}
    aria-hidden={!open}
  >
    <div class="disclosure-content">{@render children()}</div>
  </div>
</div>

<style>
  .disclosure-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.25rem 0;
    border: 0;
    background: transparent;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
    cursor: pointer;
  }
  .disclosure-toggle:hover {
    color: var(--ink);
  }
  .disclosure-toggle:focus-visible {
    outline: 2px solid var(--tone-gpu);
    outline-offset: 3px;
  }
  svg {
    flex: none;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    transition: transform 180ms ease;
  }
  svg.expanded {
    transform: rotate(90deg);
  }
  .disclosure-panel {
    display: grid;
    grid-template-rows: 0fr;
    opacity: 0;
    transition:
      grid-template-rows 180ms ease,
      opacity 180ms ease;
  }
  .disclosure-panel.expanded {
    grid-template-rows: 1fr;
    opacity: 1;
  }
  .disclosure-content {
    min-height: 0;
    overflow: hidden;
  }
  @media (prefers-reduced-motion: reduce) {
    svg,
    .disclosure-panel {
      transition: none;
    }
  }
</style>
