<script>
  let { items, label = "Breadcrumb" } = $props();
</script>

<nav aria-label={label}>
  <ol>
    {#each items as item, index}
      <li>
        {#if index > 0}<span class="separator" aria-hidden="true">/</span>{/if}
        {#if item.href}
          <a href={item.href} aria-current={item.current ? "page" : undefined}>
            {item.label}
          </a>
        {:else}
          <span aria-current={item.current ? "page" : undefined}
            >{item.label}</span
          >
        {/if}
      </li>
    {/each}
  </ol>
</nav>

<style>
  nav {
    min-width: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  ol {
    display: flex;
    flex-wrap: wrap;
    gap: 0 var(--ds-space-xs);
    padding: 0;
    margin: 0;
    list-style: none;
  }
  li {
    display: flex;
    align-items: center;
    min-width: 0;
    max-width: 100%;
    gap: var(--ds-space-xs);
  }
  a {
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    min-width: 44px;
    min-height: 44px;
    max-width: 100%;
    color: var(--ds-accent-ink, var(--ds-ink));
    text-underline-offset: 0.2em;
  }
  a:focus-visible {
    outline: var(--ds-focus-width, var(--ds-border-weight)) solid
      var(--ds-focus, var(--ds-ink));
    outline-offset: 3px;
  }
  [aria-current="page"] {
    font-weight: 700;
  }
  .separator {
    flex-shrink: 0;
  }
  @media (prefers-reduced-motion: reduce) {
    a {
      transition: none;
      animation: none;
    }
  }
</style>
