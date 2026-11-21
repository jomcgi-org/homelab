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
    background: var(--sheet);
    color: var(--ink);
  }

  .snapshot-content {
    position: absolute;
    inset: var(--snapshot-top, 1.5rem) 1.5rem var(--snapshot-bottom, 5rem);
    max-width: 36rem;
    overflow: auto;
  }

  h2 {
    margin: 0 0 0.5rem;
    font: 600 1.4rem/1.2 var(--font-ui);
  }

  p {
    font: 0.85rem/1.5 var(--font-ui);
  }

  .snapshot-summary {
    margin: 0 0 1rem;
  }

  ul {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid var(--stroke);
  }

  li {
    display: grid;
    gap: 0.2rem;
    padding: 0.65rem 0;
    border-bottom: 1px solid var(--stroke);
    font: 0.9rem/1.3 var(--font-ui);
  }

  a,
  strong {
    color: var(--ink);
    font-weight: 600;
  }

  span,
  .snapshot-note {
    font: 0.75rem/1.5 var(--font-code);
    color: var(--ink-2);
  }
</style>
