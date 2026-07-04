<script>
  // Public book view. With no `from` cursor this is the ordered section list
  // (unchanged: tapping a section still goes through chunkHref -> /c/[chunk],
  // which now redirects into the continuous reader positioned there). With a
  // `from` cursor — arrived via that same redirect, or a shared reader link —
  // this renders the continuous Reader instead, pre-loaded server-side by the
  // sibling +page.server.js.
  import { page } from "$app/stores";
  import { apiFetch, bookHref, chunkHref, libraryHref } from "$lib/public/grimoire/api.js";
  import Reader from "$lib/public/grimoire/Reader.svelte";

  let { data } = $props();

  const bookId = $derived(decodeURIComponent($page.params.book));
  const fromCursor = $derived($page.url.searchParams.get("from"));
  const anchorChunkId = $derived(
    $page.url.hash.startsWith("#c-") ? $page.url.hash.slice(3) : null,
  );
  const showReader = $derived(fromCursor !== null);

  let sections = $state([]);
  let displayName = $state("");
  let loading = $state(true);
  let error = $state("");

  $effect(() => {
    if (!showReader) load(bookId);
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
      displayName = books.find((b) => b.book_id === id)?.display_name ?? id;
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }
</script>

{#if showReader}
  <a class="eyebrow back-link reader-back" href={bookHref(bookId)}>&larr; SECTIONS</a>
  {#key `${bookId}:${fromCursor ?? ""}`}
    <Reader
      {bookId}
      items={data.items}
      nextCursor={data.nextCursor}
      {anchorChunkId}
    />
  {/key}
{:else}
  <div class="wrap book-page page">
    <a class="eyebrow back-link" href={libraryHref()}>&larr; LIBRARY</a>

    {#if !loading && !error}
      <h1 class="display book-title">{displayName}</h1>
    {/if}

    {#if loading}
      <div class="skeletons">
        {#each Array(8) as _, i (i)}
          <div class="card-hard skeleton-row"></div>
        {/each}
      </div>
    {:else if error}
      <p class="mono status-error">{error}</p>
    {:else if sections.length === 0}
      <p class="mono empty-copy">This book has no chunks yet.</p>
    {:else}
      <ul class="section-list">
        {#each sections as section (section.section_path)}
          <li>
            <a
              class="card-hard section-row"
              href={chunkHref(bookId, section.first_chunk_id)}
            >
              <span class="display section-title">{section.title}</span>
              <span class="section-meta mono">
                <span>{section.chunk_count} CHUNKS</span>
                {#if section.image_count > 0}
                  <span>{section.image_count} IMG</span>
                {/if}
              </span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}

<style>
  /* Structural only: page rhythm + the section-row flex layout, everything
     visual (.card-hard, .display, .eyebrow, .mono) is the design system.
     .card-hard is shadow-free everywhere in this app (see the grimoire
     +layout.svelte override): no box-shadows in grimoire. */
  .book-page {
    padding: 40px 32px 96px;
  }

  .back-link {
    display: inline-block;
    margin-bottom: 20px;
  }

  .back-link:hover {
    color: var(--ink);
  }

  .reader-back {
    display: inline-block;
    margin: 20px 0 0 32px;
  }

  .reader-back:hover {
    color: var(--ink);
  }

  .book-title {
    font-size: clamp(30px, 6vw, 52px);
    margin-bottom: 28px;
  }

  .section-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .section-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 18px 22px;
    min-height: 44px;
  }

  .section-title {
    font-size: 19px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .section-meta {
    flex: none;
    display: flex;
    gap: 12px;
    font-size: 11px;
    letter-spacing: 0.06em;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .empty-copy {
    color: var(--ink-3);
    padding: 32px 0;
  }

  .status-error {
    color: var(--coral);
    padding: 24px 0;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .skeleton-row {
    height: 62px;
    background: linear-gradient(
      90deg,
      var(--bg-elev) 25%,
      transparent 37%,
      var(--bg-elev) 63%
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

  @media (max-width: 640px) {
    .book-page {
      padding: 28px 16px 72px;
    }
    .section-row {
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-row {
      animation: none;
    }
  }
</style>
