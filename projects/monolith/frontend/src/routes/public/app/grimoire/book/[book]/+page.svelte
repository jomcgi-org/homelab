<script>
  // Public book view: always the continuous Reader, full-width — there is no
  // more standalone section list. Chapters navigation now lives on the
  // reader itself (ChaptersNav.svelte, in Reader's sticky bar), fed by the
  // same /books/{id}/sections API the old section list used. A bare visit
  // starts at page one; a `from` cursor — arrived via a Chapters row, a
  // shared reader link, or an entity-mention redirect — positions the reader
  // there instead. Pre-loaded server-side by the sibling +page.server.js.
  import { page } from "$app/stores";
  import Reader from "$lib/public/grimoire/Reader.svelte";

  let { data } = $props();

  const bookId = $derived(decodeURIComponent($page.params.book));
  const fromCursor = $derived($page.url.searchParams.get("from"));
  const anchorChunkId = $derived(
    $page.url.hash.startsWith("#c-") ? $page.url.hash.slice(3) : null,
  );
</script>

{#key `${bookId}:${fromCursor ?? ""}`}
  <Reader
    {bookId}
    items={data.items}
    nextCursor={data.nextCursor}
    {anchorChunkId}
  />
{/key}
