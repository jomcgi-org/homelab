<script>
  /**
   * Outer frame for an architecture diagram: a keyed figure in the
   * technical-drawing language. The caption leads as a stroke-framed title
   * row and the flow of DBox/DGroup/DArrow parts sits flush below it, so
   * inside a `.prose` body the figure's rules join the panel outline and
   * the page's figure counter numbers it. Flows horizontally on a wide
   * screen, stacks vertically under 720px.
   * @type {{ label?: string, children: import('svelte').Snippet }}
   */
  let { label = "Architecture", children } = $props();
</script>

<figure class="fig diagram" role="img" aria-label={label}>
  <figcaption>{label}</figcaption>
  <div class="fig-art">
    <div class="diagram-flow">
      {@render children()}
    </div>
  </div>
</figure>

<style>
  /* A flow wider than the measure scrolls sideways inside its panel, as
     the keyed figures do, rather than wrapping a part onto a row where no
     arrow reaches it. */
  .diagram-flow {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: flex-start;
    gap: 0.5rem;
    width: max-content;
    min-width: 100%;
    padding: 1.1rem 1rem 1rem;
  }

  @media (max-width: 720px) {
    .diagram-flow {
      flex-direction: column;
      align-items: stretch;
      width: auto;
    }
  }
</style>
