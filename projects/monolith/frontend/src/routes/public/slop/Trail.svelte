<script>
  // Slop section trail: site, section, page, and draft badge. Separate
  // component from blog/Trail.svelte because the draft cell needs special
  // styling (accent ink on a transparent background); factoring a generic
  // version would complicate both sites and risk desync. One component per
  // section is clearer.
  let { page = "" } = $props();
</script>

<nav class="trail" aria-label="You are here">
  <div class="trail-row">
    <a class="crumb" href="/">jomcgi.dev</a>
    <a class="crumb" href="/slop" aria-current={page ? undefined : "page"}
      >slop</a
    >
  </div>
  {#if page}
    <div class="trail-row">
      <span class="crumb current" aria-current="page">{page}</span>
      <span class="crumb draft">draft</span>
    </div>
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
    border-top: 1px solid var(--stroke);
    color: var(--ink);
    white-space: normal;
  }

  .crumb.draft {
    border-top: 1px solid var(--stroke);
    color: var(--accent-ink);
  }

  a.crumb:hover {
    color: var(--accent-ink);
  }

  .crumb:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }
</style>
