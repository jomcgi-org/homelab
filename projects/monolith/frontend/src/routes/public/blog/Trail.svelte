<script>
  // Where the reader is: site, blog, then the post. One box partitioned
  // by rules, the way a figure's parts are. Docked at the top of the
  // index column on a wide screen, in the chrome row on a phone.
  let { post = "" } = $props();
</script>

<nav class="trail" aria-label="You are here">
  <div class="trail-row">
    <a class="crumb" href="/">jomcgi.dev</a>
    <a class="crumb" href="/blog" aria-current={post ? undefined : "page"}
      >blog</a
    >
  </div>
  {#if post}
    <span class="crumb current" aria-current="page">{post}</span>
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

  a.crumb:hover {
    color: var(--accent-ink);
  }

  .crumb:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }
</style>
