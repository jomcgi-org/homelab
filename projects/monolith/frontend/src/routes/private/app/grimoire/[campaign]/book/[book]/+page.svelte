<script>
  // The book route: on desktop this is the Shell's right pane (the left pane
  // is always the flat TOC, SectionTree.svelte), so it always shows the
  // continuous Reader — landing on a book starts you reading page 1. On
  // mobile there is no aside, so this route doubles as the section index
  // (SectionTree, full width) until a `from` cursor is present — arriving via
  // a TOC row or an entity-mention deep link (both go through
  // c/[chunk]/+page.server.js's redirect) switches this same route into the
  // Reader, positioned at that cursor.
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import { bookHref } from "$lib/grimoire/api.js";
  import SectionTree from "$lib/grimoire/SectionTree.svelte";
  import Reader from "$lib/grimoire/Reader.svelte";

  let { data } = $props();
  const ctx = getContext("grimoire");

  const bookId = $derived(decodeURIComponent($page.params.book));
  const fromCursor = $derived($page.url.searchParams.get("from"));
  const anchorChunkId = $derived(
    $page.url.hash.startsWith("#c-") ? $page.url.hash.slice(3) : null,
  );
  const showReader = $derived(ctx.isDesktop || fromCursor !== null);
</script>

<div class="reader-page">
  {#if showReader}
    {#if !ctx.isDesktop}
      <a class="back" href={bookHref(ctx.campaignId, bookId, ctx.viewpoint)}>
        ← Sections
      </a>
    {/if}
    {#key `${bookId}:${fromCursor ?? ""}`}
      <Reader
        campaignId={ctx.campaignId}
        {bookId}
        items={data.items}
        nextCursor={data.nextCursor}
        {anchorChunkId}
      />
    {/key}
  {:else}
    <SectionTree />
  {/if}
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
