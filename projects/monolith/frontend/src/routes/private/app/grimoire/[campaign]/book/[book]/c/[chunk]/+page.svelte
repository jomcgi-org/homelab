<script>
  // Never actually shown: the sibling +page.server.js load() always throws a
  // redirect into the continuous reader before this renders. Kept as a thin
  // fallback only because SvelteKit requires a +page.svelte for the route.
</script>

<p class="redirect-note">Opening reader…</p>

<style>
  .redirect-note {
    padding: 2rem;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    color: var(--fg-tertiary);
  }
</style>
