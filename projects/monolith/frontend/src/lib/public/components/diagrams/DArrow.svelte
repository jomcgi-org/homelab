<script>
  /**
   * Flow arrow with an optional edge label. Horizontal in a desktop
   * flow; rotates to vertical when the parent flow stacks (under 720px).
   * @type {{ label?: string }}
   */
  let { label = "" } = $props();
</script>

<span class="darrow mono" aria-hidden="true">
  {#if label}<span class="darrow-label">{label}</span>{/if}
  <svg viewBox="0 0 34 12" width="34" height="12">
    <line x1="0" y1="6" x2="26" y2="6" stroke="var(--ink)" stroke-width="2.5" />
    <path d="M24,1 L33,6 L24,11" fill="none" stroke="var(--ink)" stroke-width="2.5" stroke-linejoin="round" />
  </svg>
</span>

<style>
  .darrow {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }

  .darrow-label {
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    white-space: nowrap;
  }

  @media (max-width: 720px) {
    .darrow svg {
      transform: rotate(90deg);
    }
    .darrow {
      align-self: center;
      flex-direction: row;
      gap: 6px;
    }
  }
</style>
