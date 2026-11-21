<script>
  let { title, summary, rows = [], empty, failed = false } = $props();
</script>

<section class="map-snapshot" aria-label={title}>
  <div class="snapshot-content">
    <h2>{title}</h2>
    <p class="snapshot-summary">{summary}</p>
    {#if rows.length}
      <ul>
        {#each rows as row}
          <li class="snapshot-row">
            {#if row.href}
              <a href={row.href} target="_blank" rel="noopener">{row.name}</a>
            {:else}
              <strong>{row.name}</strong>
            {/if}
            <span>{row.detail}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p>{empty}</p>
    {/if}
    {#if failed}
      <p class="snapshot-note" role="status">
        The interactive map is unavailable. This snapshot is still available.
      </p>
    {/if}
  </div>
</section>

<style>
  .map-snapshot {
    position: absolute;
    inset: 0;
    z-index: 1;
    background: var(--color-background-body);
    color: var(--color-text-primary);
  }

  .snapshot-content {
    position: absolute;
    inset: 4.5rem 1.5rem 7rem;
    max-width: 36rem;
    overflow: auto;
  }

  h2 {
    margin: 0 0 0.5rem;
    font: 600 1.4rem/1.2 var(--font-family-body);
  }

  p {
    font: 0.85rem/1.5 var(--font-family-body);
  }

  .snapshot-summary {
    margin: 0 0 1rem;
  }

  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid var(--color-border);
  }

  li {
    display: grid;
    gap: 0.2rem;
    padding: 0.65rem 0;
    border-bottom: 1px solid var(--color-border);
    font: 0.9rem/1.3 var(--font-family-body);
  }

  a,
  strong {
    color: var(--color-text-primary);
    font-weight: 600;
  }

  span,
  .snapshot-note {
    font: 0.75rem/1.5 var(--font-family-code);
    color: var(--color-text-secondary);
  }
</style>
