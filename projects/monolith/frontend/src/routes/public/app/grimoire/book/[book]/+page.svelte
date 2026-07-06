<script>
  // Public book view: always the continuous Reader, full-width — there is no
  // more standalone section list. Chapters navigation now lives on the
  // reader itself (ChaptersNav.svelte, in Reader's sticky bar), fed by the
  // same /books/{id}/sections API the old section list used. A bare visit
  // starts at page one; a `from` cursor — arrived via a Chapters row, a
  // shared reader link, or an entity-mention redirect — positions the reader
  // there instead. Pre-loaded server-side by the sibling +page.server.js.
  //
  // Adventures strip: fetched client-side (best-effort, like Reader's own
  // book-meta fetch) above the Reader for books that have been classified
  // into adventures (grimoire-classify-adventures skill). Renders nothing
  // for the vast majority of books, which have no adventure rows.
  import { page } from "$app/stores";
  import Reader from "$lib/public/grimoire/Reader.svelte";
  import { apiFetch, adventureHref } from "$lib/public/grimoire/api.js";

  let { data } = $props();

  const bookId = $derived(decodeURIComponent($page.params.book));
  const fromCursor = $derived($page.url.searchParams.get("from"));
  const anchorChunkId = $derived(
    $page.url.hash.startsWith("#c-") ? $page.url.hash.slice(3) : null,
  );

  let adventures = $state([]);

  $effect(() => {
    loadAdventures(bookId);
  });

  async function loadAdventures(id) {
    adventures = [];
    try {
      const rows = await apiFetch(
        `/books/${encodeURIComponent(id)}/adventures`,
      );
      // Only apply rows for the book currently shown, so a slow fetch for a
      // previous book can never clobber the strip after navigation.
      if (id === bookId) {
        adventures = rows;
      }
    } catch {
      // Best-effort: a failed adventures fetch must never block the reader.
      adventures = [];
    }
  }
</script>

{#if adventures.length > 0}
  <section class="wrap adventures-strip">
    <h2 class="eyebrow adventures-title">Adventures</h2>
    <ul class="adventures-list">
      {#each adventures as adv (adv.id)}
        <li>
          <a class="adventure-row" href={adventureHref(adv.id)}>
            <div class="adventure-main">
              <span class="grim-title adventure-name">{adv.name}</span>
              {#if adv.level_range}
                <span class="adventure-level">LVL {adv.level_range}</span>
              {/if}
            </div>
            {#if adv.summary}
              <p class="adventure-summary">{adv.summary}</p>
            {/if}
            <span class="adventure-count">{adv.entity_count} ENTITIES</span>
          </a>
        </li>
      {/each}
    </ul>
  </section>
{/if}

{#key `${bookId}:${fromCursor ?? ""}`}
  <Reader
    {bookId}
    items={data.items}
    nextCursor={data.nextCursor}
    {anchorChunkId}
  />
{/key}

<style>
  .adventures-strip {
    padding: 28px 32px 0;
  }

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .adventures-title {
    margin-bottom: 14px;
  }

  .adventures-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 12px;
  }

  .adventure-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 16px 20px;
    text-decoration: none;
    color: inherit;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line-soft);
    border-radius: 8px;
    transition:
      background 0.12s,
      border-color 0.12s;
  }

  .adventure-row:hover {
    background: var(--grim-surface-2);
    border-color: var(--grim-line);
  }

  .adventure-main {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
  }

  .adventure-name {
    font-size: 20px;
  }

  .adventure-level {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--grim-text-faint);
  }

  .adventure-summary {
    color: var(--grim-text-dim);
    font-size: 13px;
  }

  .adventure-count {
    font-family: var(--font-mono);
    color: var(--grim-text-faint);
    font-size: 11px;
  }
</style>
