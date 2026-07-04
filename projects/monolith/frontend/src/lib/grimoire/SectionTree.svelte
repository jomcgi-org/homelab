<script>
  // A book's ordered section list, styled as the flat brutalist TOC (see
  // Reader.svelte's docblock for the --grimb-* token rationale: this and the
  // reader are the only two components that use them, everything else in
  // grimoire keeps the oxblood/paper theme). Rendered as the desktop Shell's
  // left pane and as the mobile /book/[book] screen. Owns its own fetch so
  // neither host has to. Data flow is unchanged from before the reader
  // rework: each row still links through chunkHref -> /c/[chunk], which now
  // redirects into the continuous reader positioned at that section's start.
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

  // Indent depth from the section_path's segment count (1 = top-level
  // "chapter" row, rendered bold mono uppercase; deeper rows indent).
  function depth(sectionPath) {
    if (!sectionPath) return 1;
    return Math.max(1, sectionPath.split("/").filter(Boolean).length);
  }
</script>

<!-- See Reader.svelte: the private app doesn't load these families globally
     (the public tier's root layout does), so load them here too — this and
     Reader.svelte are the only two grimoire components using them. -->
<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link
    href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<div class="grimb-toc">
  <div class="toc-head">
    <a class="back" href={libraryHref(ctx.campaignId, ctx.viewpoint)}>
      ← Library
    </a>
    {#if !loading && !error}
      <h2 class="toc-title">{displayName}</h2>
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
        {@const d = depth(section.section_path)}
        <li>
          <a
            class="row"
            class:row--h1={d <= 1}
            class:row--active={section.first_chunk_id === activeChunkId}
            style:padding-left="{0.75 + (d - 1) * 0.9}rem"
            href={chunkHref(
              ctx.campaignId,
              bookId,
              section.first_chunk_id,
              ctx.viewpoint,
            )}
          >
            <span class="row-title">{section.title}</span>
            <span class="row-meta">
              {#if isNew(section)}<span class="row-new">new</span>{/if}
              <span class="row-count">{section.chunk_count}</span>
            </span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .grimb-toc {
    padding: 0.75rem 0;
    background: var(--grimb-cream);
    color: var(--grimb-ink);
    font-family: var(--grimb-mono);
    min-height: 100%;
  }

  .toc-head {
    padding: 0 0.85rem;
    margin-bottom: 0.85rem;
  }

  .back {
    display: inline-flex;
    align-items: center;
    min-height: 2.25rem;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grimb-ink-3);
  }

  .back:hover {
    color: var(--grimb-ink);
  }

  .toc-title {
    font-family: var(--grimb-serif);
    font-size: 1.2rem;
    color: var(--grimb-ink);
    margin-top: 0.35rem;
  }

  .sections {
    display: flex;
    flex-direction: column;
  }

  /* Flat: no boxes/borders between rows, just indentation and the active
   * row's yellow fill + left border. */
  .row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    min-height: 2.5rem;
    padding: 0.5rem 0.85rem;
    border-left: 4px solid transparent;
    color: var(--grimb-ink-3);
  }

  .row:hover {
    color: var(--grimb-ink);
  }

  .row--h1 {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--grimb-ink);
  }

  .row--active {
    background: var(--grimb-yellow);
    border-left-color: var(--grimb-ink);
    color: var(--grimb-ink);
  }

  .row-title {
    font-size: 0.85rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row--h1 .row-title {
    font-size: 0.8rem;
  }

  .row-meta {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .row-count {
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    text-align: right;
    min-width: 1.5rem;
  }

  .row-new {
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.08rem 0.3rem;
    background: var(--grimb-ink);
    color: var(--grimb-cream);
  }

  .empty {
    padding: 1.5rem 0.85rem;
    font-family: var(--grimb-serif);
    font-style: italic;
    color: var(--grimb-ink-3);
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
    padding: 0 0.85rem;
  }

  .skeleton {
    height: 2.5rem;
    margin: 0 0.85rem;
    background: linear-gradient(
      90deg,
      var(--grimb-paper) 25%,
      transparent 37%,
      var(--grimb-paper) 63%
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
