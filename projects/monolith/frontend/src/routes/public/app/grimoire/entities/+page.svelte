<script>
  // Placeholder: the entity index (type filter + search) is Task 5, a
  // follow-up. This route exists so any link into it (there are none yet from
  // Library/sections/reader) 404s gracefully rather than not at all.
  import { libraryHref } from "$lib/public/grimoire/api.js";
</script>

<div class="wrap placeholder-page page">
  <a class="eyebrow back-link" href={libraryHref()}>&larr; LIBRARY</a>
  <h1 class="display">Entities</h1>
  <p class="mono placeholder-copy">The entity index is coming soon.</p>
</div>

<style>
  .placeholder-page {
    padding: 48px 32px 96px;
  }

  .back-link {
    display: inline-block;
    margin-bottom: 20px;
  }

  .back-link:hover {
    color: var(--ink);
  }

  .placeholder-copy {
    margin-top: 16px;
    color: var(--ink-3);
  }
</style>
