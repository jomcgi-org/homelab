<script>
  // Slop has a fixed draft cell and section URL, unlike the blog trail.
  // Keeping it separate avoids adding slop-only structure to the blog.
  let { page = "" } = $props();
</script>

<nav class="trail" aria-label="You are here">
  <div class="trail-row">
    <a class="crumb" href="/">jomcgi.dev</a>
    <a class="crumb" href="/slop" aria-current={page ? undefined : "page"}
      >slop</a
    >
    {#if page}
      <span class="crumb current" aria-current="page">{page}</span>
    {/if}
    <span class="crumb draft">draft</span>
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

  .crumb.draft {
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
