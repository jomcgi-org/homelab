<script>
  let { title, level = 1, description = "", breadcrumb, actions } = $props();
</script>

<header>
  {#if breadcrumb}{@render breadcrumb()}{/if}
  <div class="row">
    <div class="heading">
      <svelte:element this={`h${level}`}>{title}</svelte:element>
      {#if description}<p>{description}</p>{/if}
    </div>
    {#if actions}<div class="actions">{@render actions()}</div>{/if}
  </div>
</header>

<style>
  header {
    min-width: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: start;
    gap: var(--ds-space-sm);
  }
  .heading {
    flex: 1 1 16rem;
    min-width: 0;
  }
  .heading :global(h1),
  .heading :global(h2),
  .heading :global(h3),
  .heading :global(h4),
  .heading :global(h5),
  .heading :global(h6) {
    margin: 0;
    font-family: var(--ds-font-display);
  }
  p {
    color: var(--ds-ink-muted);
    margin: var(--ds-space-xs) 0 0;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    min-width: 0;
    max-width: 100%;
    gap: var(--ds-space-xs);
  }
</style>
