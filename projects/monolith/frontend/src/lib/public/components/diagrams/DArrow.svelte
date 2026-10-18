<script>
  /**
   * Flow arrow with an optional edge label: a 1px ink shaft with an open
   * head, the way movement is drawn in the keyed figures. The shaft spans
   * whatever width the label needs, so a label never overruns its arrow.
   * Horizontal in a wide flow; turns vertical when the parent flow stacks
   * (under 720px).
   * @type {{ label?: string }}
   */
  let { label = "" } = $props();
</script>

<span class="darrow" aria-hidden="true">
  <span class="darrow-label">{label}</span>
  <span class="shaft">
    <span class="line"></span>
    <svg class="head" viewBox="0 0 8 12" width="8" height="12">
      <path
        d="M0.5,0.5 L7,6 L0.5,11.5"
        fill="none"
        stroke="currentColor"
        stroke-width="1"
        stroke-linejoin="miter"
      />
    </svg>
  </span>
  <span class="darrow-under"></span>
</span>

<style>
  /* Three rows of equal weight above and below the shaft keep every
     arrowhead on the row's centre line whether or not it carries a label;
     a label sitting in normal flow above the shaft used to push labelled
     arrows lower than unlabelled ones in the same drawing. */
  .darrow {
    display: inline-grid;
    flex: none;
    grid-template-rows: minmax(0.95rem, auto) 12px minmax(0.95rem, auto);
    align-items: end;
    justify-items: stretch;
    min-width: 2.2rem;
    color: var(--ink);
  }

  .darrow-label {
    padding: 0 0.35em 0.1em;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.6rem;
    letter-spacing: 0.06em;
    text-align: center;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .shaft {
    display: flex;
    align-items: center;
    width: 100%;
    height: 12px;
  }

  .line {
    flex: 1;
    height: 1px;
    background: currentColor;
  }

  .head {
    flex: none;
    display: block;
  }

  @media (max-width: 720px) {
    .darrow {
      display: flex;
      flex-direction: column;
      gap: 4px;
      align-items: center;
      align-self: center;
      min-width: 0;
    }

    .darrow-under {
      display: none;
    }

    .shaft {
      flex-direction: column;
      width: 12px;
      height: 2.2rem;
    }

    .line {
      width: 1px;
      height: auto;
    }

    .head {
      transform: rotate(90deg);
    }
  }
</style>
