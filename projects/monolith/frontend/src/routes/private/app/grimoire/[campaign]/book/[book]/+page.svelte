<script>
  // The book route: always the continuous Reader, full-width — there is no
  // more standalone section index (SectionTree.svelte, deleted) on either
  // desktop or mobile. Chapters navigation now lives on the reader itself
  // (ChaptersNav.svelte, in Reader's sticky bar). A bare visit starts at page
  // one; arriving via a Chapters row or an entity-mention deep link (both go
  // through c/[chunk]/+page.server.js's redirect) positions the reader at
  // that cursor instead.
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import Reader from "$lib/grimoire/Reader.svelte";

  let { data } = $props();
  const ctx = getContext("grimoire");

  const bookId = $derived(decodeURIComponent($page.params.book));
  const fromCursor = $derived($page.url.searchParams.get("from"));
  const anchorChunkId = $derived(
    $page.url.hash.startsWith("#c-") ? $page.url.hash.slice(3) : null,
  );
</script>

<div class="reader-page">
  {#key `${bookId}:${fromCursor ?? ""}`}
    <Reader
      campaignId={ctx.campaignId}
      {bookId}
      items={data.items}
      nextCursor={data.nextCursor}
      {anchorChunkId}
    />
  {/key}
</div>

<style>
  .reader-page {
    display: flex;
    flex-direction: column;
    min-height: 100%;
  }
</style>
