<script>
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
    return section.latest_chunk_at && (!seenAt || section.latest_chunk_at > seenAt);
  }
</script>

<div class="book">
  <a class="back" href={libraryHref(ctx.campaignId, ctx.viewpoint)}>← Library</a>

  {#if loading}
    <div class="skeletons">
      {#each Array(6) as _, i (i)}
        <div class="skeleton"></div>
      {/each}
    </div>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else}
    <h1 class="grim-title title">{displayName}</h1>

    {#if sections.length === 0}
      <p class="empty">This book has no chunks yet.</p>
    {:else}
      <ul class="sections">
        {#each sections as section (section.section_path)}
          <li>
            <a
              class="section"
              href={chunkHref(
                ctx.campaignId,
                bookId,
                section.first_chunk_id,
                ctx.viewpoint,
              )}
            >
              <span class="section-title grim-title">{section.title}</span>
              <span class="section-meta">
                <span>{section.chunk_count} chunks</span>
                {#if section.image_count > 0}
                  <span>{section.image_count} images</span>
                {/if}
                {#if isNew(section)}<span class="new">new</span>{/if}
              </span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .book {
    padding: clamp(1rem, 4vw, 2rem);
    max-width: 52rem;
    margin: 0 auto;
  }

  .back {
    display: inline-flex;
    align-items: center;
    min-height: 2.5rem;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-accent);
    margin-bottom: 0.75rem;
  }

  .title {
    font-size: clamp(1.5rem, 5vw, 2.25rem);
    color: var(--grim-accent);
    margin-bottom: 1.25rem;
  }

  .sections {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .section {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    min-height: 3rem;
    padding: 0.7rem 0.9rem;
    border: 1px solid var(--grim-paper-line);
    background: var(--bg);
    color: var(--fg);
  }

  .section:hover {
    border-color: var(--grim-accent);
  }

  .section-title {
    font-size: 1.05rem;
  }

  .section-meta {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-tertiary);
    white-space: nowrap;
  }

  .new {
    padding: 0.08rem 0.35rem;
    background: var(--grim-accent);
    color: #fff;
  }

  .empty {
    font-family: var(--grim-serif);
    font-style: italic;
    color: var(--fg-tertiary);
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .skeleton {
    height: 3rem;
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
