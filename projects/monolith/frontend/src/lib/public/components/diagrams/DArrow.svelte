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
    <path
      d="M24,1 L33,6 L24,11"
      fill="none"
      stroke="var(--ink)"
      stroke-width="2.5"
      stroke-linejoin="round"
    />
  </svg>
</span>

<style>
  /* The arrowhead (svg) is the alignment anchor: it is vertically
     centred against the box row, and the label is absolutely positioned
     ABOVE it. Stacking the label in normal flow (the old approach) shifted
     a labelled arrow's head lower than an unlabelled one, so heads landed
     at different heights within the same diagram. Anchoring on the svg
     keeps every arrowhead on the box centre line, labelled or not. */
  .darrow {
    position: relative;
    display: inline-flex;
    align-items: center;
    flex-shrink: 0;
  }

  .darrow-label {
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    margin-bottom: 2px;
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    white-space: nowrap;
    /* Opaque chip matching the diagram panel surface, so a label that is
       wider than its arrow masks the dashed group border it overlaps
       instead of visually clipping into it. */
    background: var(--paper);
    padding: 0 4px;
    border-radius: 3px;
  }

  @media (max-width: 720px) {
    .darrow svg {
      transform: rotate(90deg);
    }
    /* Stacked layout: the label sits to the side of the rotated arrow
       rather than floating above it. */
    .darrow {
      align-self: center;
      gap: 6px;
    }
    .darrow-label {
      position: static;
      transform: none;
      margin-bottom: 0;
    }
  }
</style>
