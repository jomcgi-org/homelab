<script>
  // Frame shared by the post's figures: the same caption row, rules and figure
  // counter as the static ones, then the body and an optional note. Figures
  // use div and span only: the post's p, ul, li and table rules outrank
  // component-scoped styles.
  let { title, children, note } = $props();
</script>

<figure class="fig ix">
  <figcaption>{title}</figcaption>
  <div class="ix-body">{@render children()}</div>
  {#if note}<div class="ix-note">{@render note()}</div>{/if}
</figure>

<style>
  .ix-body {
    padding: 0.75rem 1rem 1rem;
    min-width: 0;
  }
  .ix-note {
    margin: 0;
    padding: 0.55em 1rem;
    border-top: 1px solid var(--line);
    color: var(--ink-2);
    font: 0.68rem / 1.5 var(--font-code);
  }
</style>
