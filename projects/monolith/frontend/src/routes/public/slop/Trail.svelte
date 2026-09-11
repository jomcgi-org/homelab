<script>
  // Slop has its own section URL, unlike the blog trail. Keeping it separate
  // avoids adding slop-only structure to the blog.
  //
  // `page` names one leaf under /slop. `crumbs` is the deeper form: an array of
  // {label, href?} rendered in order after slop, the last one current. The two
  // are alternatives, and `page` stays because most callers only need a leaf.
  let { page = "", crumbs = null } = $props();

  const trail = $derived(
    crumbs?.length ? crumbs : page ? [{ label: page }] : [],
  );
</script>

<nav class="trail" aria-label="You are here">
  <div class="trail-row">
    <a class="crumb" href="/">jomcgi.dev</a>
    <a
      class="crumb"
      href="/slop"
      aria-current={trail.length ? undefined : "page"}>slop</a
    >
    {#each trail as crumb, index (`${crumb.label}-${index}`)}
      {#if index === trail.length - 1}
        <span class="crumb current" aria-current="page">{crumb.label}</span>
      {:else if crumb.href}
        <a class="crumb" href={crumb.href}>{crumb.label}</a>
      {:else}
        <span class="crumb">{crumb.label}</span>
      {/if}
    {/each}
  </div>
</nav>

<style>
  .trail {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--stroke);
    background: var(--sheet);
  }

  .trail-row {
    display: flex;
  }

  .trail-row .crumb:first-child {
    flex: 1;
  }

  .crumb {
    padding: 5px 8px;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    text-decoration: none;
    white-space: nowrap;
  }

  .trail-row .crumb + .crumb {
    border-left: 1px solid var(--stroke);
  }

  .crumb.current {
    color: var(--ink);
    white-space: normal;
  }

  a.crumb:hover {
    color: var(--accent-ink);
  }

  .crumb:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }
</style>
