<script>
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import { bookHref } from "$lib/grimoire/api.js";
  import ChunkReader from "$lib/grimoire/ChunkReader.svelte";

  const ctx = getContext("grimoire");
  const bookId = $derived(decodeURIComponent($page.params.book));
  const chunkId = $derived($page.params.chunk);
</script>

<div class="reader-page">
  <a class="back" href={bookHref(ctx.campaignId, bookId, ctx.viewpoint)}>
    ← Sections
  </a>
  <ChunkReader
    campaignId={ctx.campaignId}
    {bookId}
    {chunkId}
    viewpoint={ctx.viewpoint}
  />
</div>

<style>
  .reader-page {
    display: flex;
    flex-direction: column;
    min-height: 100%;
  }

  .back {
    display: inline-flex;
    align-items: center;
    min-height: 2.5rem;
    padding: 0 clamp(1rem, 4vw, 2rem);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-accent);
    flex-shrink: 0;
  }
</style>
