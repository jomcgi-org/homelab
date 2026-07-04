<script>
  // Placeholder: entity detail / stat blocks is Task 5, a follow-up. The
  // chunk reader's "on this page" chips already link here (entityHref), so
  // this route exists to land those clicks somewhere real rather than a 404.
  import { page } from "$app/stores";
  import { libraryHref } from "$lib/public/grimoire/api.js";

  const id = $derived($page.params.id);
</script>

<div class="wrap placeholder-page page">
  <a class="eyebrow back-link" href={libraryHref()}>&larr; LIBRARY</a>
  <h1 class="display">Entity #{id}</h1>
  <p class="mono placeholder-copy">Stat blocks are coming soon.</p>
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
