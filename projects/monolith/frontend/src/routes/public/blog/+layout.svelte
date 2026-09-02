<script>
  import "$lib/public/styles/technical-drawing.css";

  let { children } = $props();
</script>

<a class="td back-to-site" href="/">&larr; jomcgi.dev</a>

{@render children()}

<style>
  .back-to-site {
    position: fixed;
    top: 1.1rem;
    left: 1.2rem;
    z-index: 1000;
    padding: 4px 6px;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    text-decoration: none;
  }

  .back-to-site:hover {
    color: var(--accent-ink);
  }

  .back-to-site:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
</style>
