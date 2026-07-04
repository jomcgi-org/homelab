<script>
  // Public Grimoire Library: every loaded book plus a static corpus stats
  // strip at the top. Fetches AFTER the layout's TurnstileGate admits
  // (this component only mounts once `admitted` is true), so there is no
  // corpus fetch before the challenge is solved.
  import { onMount } from "svelte";
  import Sticker from "$lib/public/components/Sticker.svelte";
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

  // NEW badge: a book with a chunk loaded in the last 7 days. Public visitors
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

  // Static corpus totals for the strip at the top: aggregate across every
  // loaded book, with the most recent sync time. Static and readable, not a
  // scrolling marquee.
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

<div class="library-page page">
  {#if !loading && !error && books.length > 0}
    <div class="stats-strip mono" role="status">
      <span>{totals.books} {totals.books === 1 ? "BOOK" : "BOOKS"}</span>
      <span class="strip-dot" aria-hidden="true">●</span>
      <span>{totals.chunks} CHUNKS</span>
      <span class="strip-dot" aria-hidden="true">●</span>
      <span>{totals.images} IMAGES</span>
      <span class="strip-dot" aria-hidden="true">●</span>
      <span>{totals.entities} ENTITIES</span>
      <span class="strip-dot" aria-hidden="true">●</span>
      <span>SYNCED {totals.synced}</span>
    </div>
  {/if}

  <div class="wrap">
    <p class="eyebrow">THE GRIMOIRE</p>
    <h1 class="display lib-title">Library</h1>

  {#if loading}
    <div class="skeletons">
      {#each [1, 2, 3] as n (n)}
        <div class="card-hard skeleton-row"></div>
      {/each}
    </div>
  {:else if error}
    <p class="mono status-error">{error}</p>
  {:else if books.length === 0}
    <div class="empty">
      <p class="display empty-lead">Nothing loaded yet.</p>
      <p class="mono empty-help">Check back once a sourcebook has been uploaded.</p>
    </div>
  {:else}
    <ul class="book-list">
      {#each books as book (book.book_id)}
        <li class="card-hard book-row">
          <div class="book-main">
            <div class="book-name-row">
              <span class="display book-name">{book.display_name}</span>
              {#if isNew(book)}
                <Sticker color="var(--accent)" rotate={-4}>NEW</Sticker>
              {/if}
            </div>

            <div class="book-stats">
              <span class="chip mono">{book.chunk_count} CHUNKS</span>
              <span class="chip mono">{book.image_count} IMAGES</span>
              <span class="chip mono">{book.entity_count} ENTITIES</span>
              <span class="chip mono chip-muted"
                >SYNCED {timeAgo(book.latest_chunk_at)}</span
              >
            </div>
          </div>

          <a class="btn btn-primary book-read" href={bookHref(book.book_id)}>
            READ
          </a>
        </li>
      {/each}
    </ul>
  {/if}
  </div>
</div>

<style>
  /* Everything visual (cards, buttons, headings, highlight) comes from the
     design system. The custom rules below are purely structural: page
     vertical rhythm, the book-row flex layout, and two small primitives the
     design system doesn't ship (stat chip pills, a coverage bar) built from
     its own tokens rather than new colors. */
  .library-page {
    padding-bottom: 96px;
  }

  /* Static full-bleed stats strip: the ticker's useful numbers without the
     scroll. Accent ground, hard rule below, wraps on small screens. */
  .stats-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    column-gap: 14px;
    row-gap: 4px;
    padding: 10px 16px;
    background: var(--accent);
    border-bottom: 2px solid var(--ink);
    color: var(--ink);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
  }

  .strip-dot {
    font-size: 8px;
  }

  .lib-title {
    font-size: clamp(36px, 7vw, 64px);
    margin: 4px 0 28px;
  }

  .library-page .wrap {
    padding-top: 40px;
  }

  .book-list {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .book-row {
    padding: 24px;
    display: flex;
    align-items: center;
    gap: 20px;
  }

  .book-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .book-name-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .book-name {
    font-size: clamp(20px, 3vw, 28px);
  }

  .book-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 5px 10px;
    border: 2px solid var(--ink);
    background: var(--bg-elev);
    color: var(--ink);
  }

  .chip-muted {
    background: transparent;
    color: var(--ink-3);
    border-color: var(--rule-2);
  }

  .book-read {
    flex: none;
    min-height: 44px;
  }

  .empty {
    padding: 64px 0;
  }

  .empty-lead {
    font-size: 28px;
    margin-bottom: 10px;
  }

  .empty-help {
    color: var(--ink-3);
    font-size: 13px;
  }

  .status-error {
    color: var(--coral);
    padding: 24px 0;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .skeleton-row {
    height: 110px;
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
    .library-page {
      padding-bottom: 72px;
    }
    .library-page .wrap {
      padding-top: 28px;
    }
    .book-row {
      flex-direction: column;
      align-items: stretch;
    }
    .book-read {
      align-self: flex-start;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-row {
      animation: none;
    }
  }
</style>
