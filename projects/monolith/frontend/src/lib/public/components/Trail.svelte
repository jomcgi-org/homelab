<script>
  // Where the reader is: the site, then each section, then the page being
  // read. One box partitioned by rules, the way a figure's parts are. Pages
  // dock it at the top of the spine on a wide screen; the chrome row
  // carries it on a phone.
  //
  // `crumbs` is the row after the site root, {label, href?} in order. The
  // last crumb is the current place unless `current` names a page of its
  // own, which then takes a second row so a long title wraps under the row
  // instead of stretching it.
  let { crumbs = [], current = "", label = "You are here" } = $props();

  const rootIsCurrent = $derived(!crumbs.length && !current);
</script>

<nav class="trail" aria-label={label}>
  <div class="trail-row">
    <a
      class="crumb"
      class:current={rootIsCurrent}
      href="/"
      aria-current={rootIsCurrent ? "page" : undefined}>jomcgi.dev</a
    >
    {#each crumbs as crumb, index (`${crumb.label}-${index}`)}
      {@const last = index === crumbs.length - 1 && !current}
      {#if crumb.href}
        <a
          class="crumb"
          class:current={last}
          href={crumb.href}
          aria-current={last ? "page" : undefined}>{crumb.label}</a
        >
      {:else}
        <span
          class="crumb"
          class:current={last}
          aria-current={last ? "page" : undefined}>{crumb.label}</span
        >
      {/if}
    {/each}
  </div>
  {#if current}
    <span class="crumb current title" aria-current="page">{current}</span>
  {/if}
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
  }

  .crumb.title {
    border-top: 1px solid var(--stroke);
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
