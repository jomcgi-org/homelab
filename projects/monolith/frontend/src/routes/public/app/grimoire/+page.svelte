<script>
  // Public Grimoire Library: every loaded book, plus a quiet one-line stats
  // summary at the top. Fetches AFTER the layout's TurnstileGate admits
  // (this component only mounts once `admitted` is true), so there is no
  // corpus fetch before the challenge is solved.
  //
  // NOTE ON GROUPING: the 2026-07-05 reskin mockup groups rows by book_kind
  // (Adventures / Anthologies / Setting Guides / Bestiaries / Spellbooks /
  // Magic Items / Rulebooks). The public GET /books response
  // (grimoire.library.list_books, mounted at router_public) does not carry a
  // book_kind field today -- the Book model has no such column -- so this page
  // deliberately renders a single flat, ungrouped list rather than inventing a
  // client-side title->kind mapping. Add book_kind grouping here once the
  // backend response includes it.
  import { onMount } from "svelte";
  import { apiFetch, bookHref } from "$lib/public/grimoire/api.js";

  let books = $state([]);
  let loading = $state(true);
  let error = $state("");

  onMount(load);

  async function load() {
    loading = true;
    error = "";
    try {
      books = await apiFetch("/books");
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  function timeAgo(iso) {
    if (!iso) return "never";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const secs = Math.max(0, (Date.now() - then) / 1000);
    const units = [
      ["y", 31536000],
      ["mo", 2592000],
      ["d", 86400],
      ["h", 3600],
      ["m", 60],
    ];
    for (const [label, size] of units) {
      if (secs >= size) return `${Math.floor(secs / size)}${label} ago`;
    }
    return "just now";
  }

  // NEW pill: a book with a chunk loaded in the last 7 days. Public visitors
  // are anonymous, so there is no per-device "last seen" bookkeeping (that was
  // a private-app-only localStorage convenience); a fixed recency window keeps
  // this simple and stateless.
  const NEW_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
  function isNew(book) {
    if (!book.latest_chunk_at) return false;
    const then = new Date(book.latest_chunk_at).getTime();
    if (Number.isNaN(then)) return false;
    return Date.now() - then < NEW_WINDOW_MS;
  }

  // Corpus totals for the one-line summary: aggregate across every loaded
  // book, with the most recent sync time.
  const totals = $derived.by(() => {
    const sum = (key) => books.reduce((acc, b) => acc + (b[key] ?? 0), 0);
    const latest = books
      .map((b) => b.latest_chunk_at)
      .filter(Boolean)
      .sort()
      .at(-1);
    return {
      books: books.length,
      chunks: sum("chunk_count"),
      images: sum("image_count"),
      entities: sum("entity_count"),
      synced: timeAgo(latest),
    };
  });
</script>

<div class="library-page">
  <p class="eyebrow">The Grimoire</p>
  <h1 class="grim-title lib-title">Library</h1>

  {#if !loading && !error && books.length > 0}
    <p class="summary">
      <b>{totals.books.toLocaleString()}</b>
      {totals.books === 1 ? "book" : "books"}
      <span class="dot">/</span>
      <b>{totals.chunks.toLocaleString()}</b> chunks
      <span class="dot">/</span>
      <b>{totals.images.toLocaleString()}</b> images
      <span class="dot">/</span>
      <b>{totals.entities.toLocaleString()}</b> entities
      <span class="dot">/</span>
      synced {totals.synced}
    </p>
  {/if}

  {#if loading}
    <div class="skeletons" aria-hidden="true">
      {#each [1, 2, 3, 4, 5] as n (n)}
        <div class="skeleton-row"></div>
      {/each}
    </div>
  {:else if error}
    <p class="status-error">{error}</p>
  {:else if books.length === 0}
    <div class="empty">
      <p class="grim-title empty-lead">Nothing loaded yet.</p>
      <p class="empty-help">Check back once a sourcebook has been uploaded.</p>
    </div>
  {:else}
    <ul class="book-list">
      {#each books as book (book.book_id)}
        <li>
          <a class="brow" href={bookHref(book.book_id)}>
            <span class="brow-main">
              <span class="title">
                {book.display_name}
                {#if isNew(book)}<span class="pill">New</span>{/if}
              </span>
              <span class="meta">
                {book.chunk_count.toLocaleString()} chunks
                <span class="dot">/</span>
                {book.image_count.toLocaleString()} images
                <span class="dot">/</span>
                {book.entity_count.toLocaleString()} entities
              </span>
            </span>
            <span class="read">Read &rarr;</span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .library-page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 40px 28px 80px;
  }

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .lib-title {
    font-size: clamp(36px, 6vw, 46px);
    margin: 6px 0 0;
  }

  .summary {
    margin-top: 14px;
    color: var(--grim-text-dim);
    font-size: 13.5px;
    font-variant-numeric: tabular-nums;
  }

  .summary b {
    color: var(--grim-ink);
    font-weight: 600;
  }

  .dot {
    color: var(--grim-text-faint);
    margin: 0 8px;
  }

  .book-list {
    list-style: none;
    margin: 24px 0 0;
    padding: 0;
  }

  .brow {
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: 15px;
    padding: 13px 8px;
    text-decoration: none;
    color: inherit;
    border-bottom: 1px solid var(--grim-line-soft);
    border-radius: 7px;
  }

  .book-list li:last-child .brow {
    border-bottom: 0;
  }

  .brow:hover {
    background: var(--grim-surface-2);
  }

  .brow .title {
    font-family: var(--grim-serif);
    font-size: 18px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 9px;
    color: var(--grim-ink);
  }

  .brow .meta {
    display: block;
    margin-top: 3px;
    color: var(--grim-text-dim);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }

  .brow .meta .dot {
    margin: 0 6px;
  }

  .pill {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--grim-accent);
    background: var(--grim-accent-soft);
    border-radius: 4px;
    padding: 2px 6px;
  }

  .read {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    white-space: nowrap;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--grim-accent);
    opacity: 0;
    transition: opacity 0.15s;
  }

  .brow:hover .read {
    opacity: 1;
  }

  .empty {
    padding: 64px 0;
  }

  .empty-lead {
    font-size: 28px;
    margin-bottom: 10px;
  }

  .empty-help {
    color: var(--grim-text-dim);
    font-size: 13px;
  }

  .status-error {
    color: var(--grim-type-creature);
    padding: 24px 0;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 24px;
  }

  .skeleton-row {
    height: 62px;
    border-radius: 7px;
    background: linear-gradient(
      90deg,
      var(--grim-surface-2) 25%,
      transparent 37%,
      var(--grim-surface-2) 63%
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
    .library-page {
      padding: 28px 20px 60px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-row {
      animation: none;
    }
  }
</style>
