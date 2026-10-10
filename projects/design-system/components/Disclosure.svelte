<script>
  let { open = $bindable(false), summary, children } = $props();
</script>

<details bind:open>
  <summary>
    <span class="indicator" aria-hidden="true"></span>
    <span>{@render summary()}</span>
  </summary>
  <div class="content">
    {#if children}{@render children()}{/if}
  </div>
</details>

<style>
  details {
    border: var(--ds-border-weight) solid var(--ds-line-strong);
    border-radius: var(--ds-radius);
    background: var(--ds-surface);
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  summary {
    box-sizing: border-box;
    display: flex;
    align-items: center;
    gap: var(--ds-space-xs);
    min-width: 44px;
    min-height: 44px;
    padding: var(--ds-space-xs) var(--ds-space-sm);
    list-style: none;
    cursor: pointer;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary:focus-visible {
    outline: var(--ds-focus-width, var(--ds-border-weight)) solid
      var(--ds-focus, var(--ds-ink));
    outline-offset: 3px;
  }
  .indicator {
    position: relative;
    flex: 0 0 1em;
    width: 1em;
    height: 1em;
  }
  .indicator::before,
  .indicator::after {
    content: "";
    position: absolute;
    inset: calc(50% - 1px) 0 auto;
    border-top: 2px solid currentColor;
  }
  .indicator::after {
    transform: rotate(90deg);
  }
  details[open] .indicator::after {
    display: none;
  }
  .content {
    padding: var(--ds-space-sm);
    border-top: var(--ds-border-weight) solid var(--ds-line);
  }
  @media (prefers-reduced-motion: reduce) {
    summary,
    .indicator {
      transition: none;
      animation: none;
    }
  }
</style>
