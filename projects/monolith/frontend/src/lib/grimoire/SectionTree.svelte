<script>
  // A book's ordered section list. Rendered as the desktop Shell's left pane and
  // as the mobile /book/[book] screen. Owns its own fetch so neither host has to.
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import {
    apiFetch,
    chunkHref,
    libraryHref,
    bookLastSeen,
    markBookSeen,
  } from "$lib/grimoire/api.js";

  const ctx = getContext("grimoire");

  const bookId = $derived(decodeURIComponent($page.params.book));
  // The chunk currently open in the reading pane, so we can mark its section.
  const activeChunkId = $derived($page.params.chunk ?? "");

  let sections = $state([]);
  let displayName = $state("");
  let loading = $state(true);
  let error = $state("");

  $effect(() => {
    load(bookId);
  });

  async function load(id) {
    loading = true;
    error = "";
    try {
      const [secs, books] = await Promise.all([
        apiFetch(`/books/${encodeURIComponent(id)}/sections`),
        apiFetch("/books"),
      ]);
      sections = secs;
      const book = books.find((b) => b.book_id === id);
      displayName = book?.display_name ?? id;
      // Visiting the book counts as "seen" for the Library's new-badge.
      if (book?.latest_chunk_at) markBookSeen(id, book.latest_chunk_at);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  const seenAt = $derived(bookLastSeen(bookId));
  function isNew(section) {
    return (
      section.latest_chunk_at && (!seenAt || section.latest_chunk_at > seenAt)
    );
  }
</script>

<div class="tree">
  <div class="tree-head">
    <a class="back" href={libraryHref(ctx.campaignId, ctx.viewpoint)}>
      ← Library
    </a>
    {#if !loading && !error}
      <h2 class="grim-title title">{displayName}</h2>
    {/if}
  </div>

  {#if loading}
    <ul class="sections">
      {#each Array(8) as _, i (i)}
        <li class="skeleton"></li>
      {/each}
    </ul>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else if sections.length === 0}
    <p class="empty">This book has no chunks yet.</p>
  {:else}
    <ul class="sections">
      {#each sections as section (section.section_path)}
        <li>
          <a
            class="section"
            class:section--active={section.first_chunk_id === activeChunkId}
            href={chunkHref(
              ctx.campaignId,
              bookId,
              section.first_chunk_id,
              ctx.viewpoint,
            )}
          >
            <span class="section-title grim-title">{section.title}</span>
            <span class="section-meta">
              <span>{section.chunk_count}</span>
              {#if section.image_count > 0}
                <span>{section.image_count} img</span>
              {/if}
              {#if isNew(section)}<span class="new">new</span>{/if}
            </span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .tree {
    padding: 0.75rem;
  }

  .tree-head {
    margin-bottom: 0.85rem;
  }

  .back {
    display: inline-flex;
    align-items: center;
    min-height: 2.25rem;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-accent);
  }

  .title {
    font-size: 1.15rem;
    color: var(--grim-accent);
    margin-top: 0.25rem;
  }

  .sections {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .section {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    min-height: 2.75rem;
    padding: 0.45rem 0.6rem;
    border: 1px solid transparent;
    border-left: 2px solid transparent;
    background: var(--bg);
    color: var(--fg);
  }

  .section:hover {
    border-color: var(--grim-paper-line);
    border-left-color: var(--grim-accent);
  }

  .section--active {
    border-color: var(--grim-paper-line);
    border-left-color: var(--grim-accent);
    background: var(--surface);
  }

  .section-title {
    font-size: 0.98rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .section-meta {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-tertiary);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .new {
    padding: 0.08rem 0.35rem;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
  }

  .empty {
    padding: 1.5rem 0.6rem;
    font-family: var(--grim-serif);
    font-style: italic;
    color: var(--fg-tertiary);
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
  }

  .skeleton {
    height: 2.75rem;
    background: linear-gradient(
      90deg,
      var(--surface) 25%,
      transparent 37%,
      var(--surface) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease infinite;
  }

  @keyframes shimmer {
    0% {
      background-position: 100% 0;
    }
    100% {
      background-position: 0 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton {
      animation: none;
    }
  }
</style>
