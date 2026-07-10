<script>
  // Public Grimoire Library: every loaded book, plus a quiet one-line stats
  // summary at the top. Fetches AFTER the layout's TurnstileGate admits
  // (this component only mounts once `admitted` is true), so there is no
  // corpus fetch before the challenge is solved.
  //
  // Rows are grouped by book_kind (Adventures / Anthologies / Setting Guides /
  // Bestiaries / Spellbooks / Magic Items / Rulebooks). book_kind is derived
  // server-side from the slug (grimoire.extract.book_kind) and returned by GET
  // /books; unmapped slugs fall into an "Other" bucket.
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

  // Book-type grouping (book_kind comes from GET /books, derived from the slug).
  const KIND_LABEL = {
    adventure: "Adventures",
    "adventure-anthology": "Anthologies",
    "setting-guide": "Setting Guides",
    bestiary: "Bestiaries",
    spellbook: "Spellbooks",
    "magic-items": "Magic Items",
    rulebook: "Rulebooks",
    other: "Other",
  };
  const KIND_ORDER = [
    "adventure",
    "adventure-anthology",
    "setting-guide",
    "bestiary",
    "spellbook",
    "magic-items",
    "rulebook",
    "other",
  ];
  // Filter: "all" shows the whole corpus; "readable" collapses to just the
  // open-licensed books you can actually open in full. Only a couple of books
  // are readable, so this is the shortcut that surfaces them out of the
  // otherwise-long locked list.
  let filter = $state("all");
  const readableCount = $derived(
    books.filter((b) => !b.copyrighted_content).length,
  );

  const grouped = $derived.by(() => {
    const visible =
      filter === "readable"
        ? books.filter((b) => !b.copyrighted_content)
        : books;
    const by = {};
    for (const b of visible) {
      const k = KIND_LABEL[b.book_kind] ? b.book_kind : "other";
      (by[k] ??= []).push(b);
    }
    return KIND_ORDER.filter((k) => by[k]?.length).map((k) => ({
      kind: k,
      label: KIND_LABEL[k],
      rows: by[k],
    }));
  });
</script>

<div class="library-page">
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
    <p class="legend">
      Open-licensed books are readable in full. Others are listed for reference.
    </p>
    {#if readableCount > 0 && readableCount < totals.books}
      <div class="filter" role="tablist" aria-label="Filter books">
        <button
          class="filter-btn"
          class:active={filter === "all"}
          role="tab"
          aria-selected={filter === "all"}
          onclick={() => (filter = "all")}
        >
          All <span class="filter-n">{totals.books}</span>
        </button>
        <button
          class="filter-btn"
          class:active={filter === "readable"}
          role="tab"
          aria-selected={filter === "readable"}
          onclick={() => (filter = "readable")}
        >
          Readable <span class="filter-n">{readableCount}</span>
        </button>
      </div>
    {/if}
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
    {#each grouped as group (group.kind)}
      <section class="lib-group">
        <div class="gh">
          <span class="kind">{group.label}</span>
          <span class="kn">{group.rows.length}</span>
        </div>
        <ul class="book-list">
          {#each group.rows as book (book.book_id)}
            <li>
              {#snippet body()}
                <span class="brow-main">
                  <span class="title">
                    {book.display_name}
                    {#if !book.copyrighted_content && isNew(book)}
                      <span class="pill">New</span>
                    {/if}
                  </span>
                  <span class="meta">
                    {book.chunk_count.toLocaleString()} chunks
                    <span class="dot">/</span>
                    {book.image_count.toLocaleString()} images
                    <span class="dot">/</span>
                    {book.entity_count.toLocaleString()} entities
                  </span>
                </span>
              {/snippet}
              {#if book.copyrighted_content}
                <!-- Listed for reference (breadth of the corpus is the
                     showcase) but not readable in full: a quiet lock, no loud
                     label. Not a link; the backend also 403s the read
                     endpoints. -->
                <div class="brow locked" title="Not available to read here">
                  {@render body()}
                  <span class="lock" aria-label="Locked">
                    <svg
                      viewBox="0 0 16 16"
                      width="14"
                      height="14"
                      fill="none"
                      aria-hidden="true"
                    >
                      <rect
                        x="3"
                        y="7"
                        width="10"
                        height="7"
                        rx="1.5"
                        stroke="currentColor"
                        stroke-width="1.3"
                      />
                      <path
                        d="M5 7V5a3 3 0 0 1 6 0v2"
                        stroke="currentColor"
                        stroke-width="1.3"
                      />
                    </svg>
                  </span>
                </div>
              {:else}
                <a class="brow" href={bookHref(book.book_id)}>
                  {@render body()}
                  <span class="read">Read &rarr;</span>
                </a>
              {/if}
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {/if}
</div>

<style>
  .library-page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 40px 28px 80px;
  }

  .lib-title {
    font-size: clamp(36px, 6vw, 46px);
    margin: 0;
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

  .legend {
    margin-top: 8px;
    max-width: 620px;
    color: var(--grim-text-faint);
    font-size: 12px;
    line-height: 1.5;
  }

  /* Segmented All / Readable filter: a quiet pill pair on a hairline track. */
  .filter {
    display: inline-flex;
    margin-top: 16px;
    padding: 3px;
    gap: 3px;
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    border-radius: 8px;
  }

  .filter-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--grim-text-dim);
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition:
      background 0.12s,
      color 0.12s;
  }

  .filter-btn:hover {
    color: var(--grim-ink);
  }

  .filter-btn.active {
    background: var(--grim-paper);
    color: var(--grim-ink);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
  }

  .filter-n {
    font-variant-numeric: tabular-nums;
    color: var(--grim-text-faint);
  }

  .filter-btn.active .filter-n {
    color: var(--grim-accent);
  }

  .dot {
    color: var(--grim-text-faint);
    margin: 0 8px;
  }

  .lib-group {
    margin-top: 32px;
  }

  .gh {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 0 8px 8px;
    border-bottom: 1px solid var(--grim-line);
  }

  .gh .kind {
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--grim-accent);
    font-weight: 700;
  }

  .gh .kn {
    font-size: 11px;
    color: var(--grim-text-faint);
    font-variant-numeric: tabular-nums;
  }

  .book-list {
    list-style: none;
    margin: 2px 0 0;
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

  /* Copyrighted books are listed but Reader-locked: dim the whole row and drop
     the hover affordance so it reads as inert, not clickable. */
  .brow.locked {
    cursor: default;
    opacity: 0.62;
  }

  .brow.locked:hover {
    background: transparent;
  }

  /* Quiet lock on locked rows: the single, low-key marker (no "Copyrighted"
     pill, no "Locked" label). Sits where "Read ->" would be on open rows. */
  .lock {
    display: inline-flex;
    align-items: center;
    color: var(--grim-text-faint);
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
