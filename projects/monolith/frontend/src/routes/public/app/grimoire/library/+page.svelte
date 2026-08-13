<script>
  // Public Grimoire Library: a shelf of every loaded book, plus a quiet
  // one-line stats summary at the top. Fetches AFTER the layout's
  // TurnstileGate admits (this component only mounts once `admitted` is
  // true), so there is no corpus fetch before the challenge is solved.
  //
  // Books as objects: each book renders as a cover card (real page image
  // when one exists, a generated typographic cover otherwise), grouped by
  // book_kind (Adventures / Anthologies / Setting Guides / Bestiaries /
  // Spellbooks / Magic Items / Rulebooks). book_kind is derived server-side
  // from the slug (grimoire.extract.book_kind) and returned by GET /books;
  // unmapped slugs fall into an "Other" bucket.
  import { onMount } from "svelte";
  import { apiFetch, bookHref, API } from "$lib/public/grimoire/api.js";

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
      console.error("Could not load library", e);
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

  // Generated-cover hue: a tiny deterministic hash of book_id picked into a
  // small palette of muted, parchment-compatible tokens (see theme.css). Not
  // cryptographic, just a stable pick per book so the same book always gets
  // the same tint.
  const COVER_HUES = [
    "--grim-cover-1",
    "--grim-cover-2",
    "--grim-cover-3",
    "--grim-cover-4",
    "--grim-cover-5",
    "--grim-cover-6",
  ];
  function hashHue(bookId) {
    let h = 0;
    for (let i = 0; i < bookId.length; i++) {
      h = (h * 31 + bookId.charCodeAt(i)) | 0;
    }
    return COVER_HUES[Math.abs(h) % COVER_HUES.length];
  }

  // Up to 3 initials from the display name, for the generated cover.
  function initials(name) {
    const words = name.split(/\s+/).filter(Boolean);
    if (words.length === 0) return "?";
    return words
      .slice(0, 3)
      .map((w) => w[0].toUpperCase())
      .join("");
  }

  function coverImageUrl(book) {
    return `${API}/chunks/${encodeURIComponent(book.cover_chunk_id)}/image`;
  }
</script>

<div class="library-page">
  <h1 class="grim-title lib-title">Library</h1>

  {#if !loading && !error && books.length > 0}
    <p class="summary">
      {totals.books.toLocaleString()}
      {totals.books === 1 ? "book" : "books"}, {totals.chunks.toLocaleString()}
      pages of lore, {totals.entities.toLocaleString()} people and places{#if totals.synced !== "never"},
        updated {totals.synced}{/if}
    </p>
    <p class="legend">
      Some books are free to read here in full. The rest are reference only.
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
      {#each [1, 2, 3, 4, 5, 6] as n (n)}
        <div class="skeleton-card"></div>
      {/each}
    </div>
  {:else if error}
    <p class="status-error">
      Could not load this right now. Try again in a moment.
    </p>
  {:else if books.length === 0}
    <div class="empty">
      <p class="grim-title empty-lead">No books here yet.</p>
      <p class="empty-help">Check back soon.</p>
    </div>
  {:else}
    {#each grouped as group (group.kind)}
      <section class="lib-group">
        <div class="gh">
          <span class="kind">{group.label}</span>
          <span class="kn">{group.rows.length}</span>
        </div>
        <div class="shelf">
          {#each group.rows as book (book.book_id)}
            <a
              class="card"
              class:locked={book.copyrighted_content}
              href={bookHref(book.book_id)}
              aria-disabled={book.copyrighted_content}
              title={book.copyrighted_content
                ? "Reference only, not available to read here"
                : undefined}
            >
              <span class="cover" class:muted={book.copyrighted_content}>
                {#if book.cover_chunk_id}
                  <img
                    src={coverImageUrl(book)}
                    alt="{book.display_name} cover"
                    loading="lazy"
                  />
                {:else}
                  <span
                    class="cover-generated"
                    style={`background: var(${hashHue(book.book_id)})`}
                  >
                    {initials(book.display_name)}
                  </span>
                {/if}
                {#if book.copyrighted_content}
                  <span class="badge locked-badge">
                    <svg
                      viewBox="0 0 16 16"
                      width="10"
                      height="10"
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
                    <span>Reference only</span>
                  </span>
                {:else}
                  <span class="badge read-badge" aria-hidden="true">
                    Read &rarr;
                  </span>
                {/if}
              </span>
              <span class="card-body">
                <span class="title">
                  {book.display_name}
                  {#if !book.copyrighted_content && isNew(book)}
                    <span class="pill">New</span>
                  {/if}
                </span>
                <span class="kind-label grim-smallcaps">{group.label}</span>
                <span class="chips">
                  <span class="chip"
                    >{book.entity_count.toLocaleString()} people and places</span
                  >
                  <span class="chip"
                    >{book.image_count.toLocaleString()} images</span
                  >
                </span>
              </span>
            </a>
          {/each}
        </div>
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

  /* Shelf grid: cover cards, responsive columns, no horizontal overflow even
     on a 390px mobile viewport. */
  .shelf {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 20px;
    margin-top: 16px;
  }

  @media (min-width: 900px) {
    .shelf {
      grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
      gap: 24px;
    }
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: 9px;
    text-decoration: none;
    color: inherit;
    border-radius: 10px;
  }

  .cover {
    position: relative;
    display: block;
    aspect-ratio: 2 / 3;
    border-radius: 8px;
    overflow: hidden;
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    transition:
      transform 0.16s ease-out,
      box-shadow 0.16s ease-out;
  }

  .cover img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  .cover.muted img {
    filter: saturate(0.6) opacity(0.85);
  }

  .cover-generated {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    font-family: var(--grim-serif);
    font-size: clamp(22px, 5vw, 30px);
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--grim-on-accent);
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
  }

  .cover.muted .cover-generated {
    filter: saturate(0.6) opacity(0.85);
  }

  /* Readable books get a page-lift on hover: transform/shadow only. */
  .card:not(.locked):hover .cover {
    transform: translateY(-4px);
    box-shadow: 0 10px 18px rgba(0, 0, 0, 0.14);
  }

  .card.locked {
    cursor: default;
  }

  .badge {
    position: absolute;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-radius: 5px;
    padding: 3px 6px;
    line-height: 1.3;
  }

  .locked-badge {
    top: 6px;
    left: 6px;
    color: var(--grim-ink);
    background: rgba(255, 255, 255, 0.88);
    border: 1px solid var(--grim-line);
  }

  .read-badge {
    right: 6px;
    bottom: 6px;
    left: 6px;
    justify-content: center;
    color: var(--grim-on-accent);
    background: var(--grim-accent);
    opacity: 0;
    transform: translateY(4px);
    transition:
      opacity 0.15s,
      transform 0.15s;
  }

  .card:not(.locked):hover .read-badge {
    opacity: 1;
    transform: none;
  }

  .card-body {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .title {
    font-family: var(--grim-serif);
    font-size: 15px;
    font-weight: 600;
    line-height: 1.25;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    color: var(--grim-ink);
  }

  .kind-label {
    font-size: 10px;
    color: var(--grim-text-faint);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 2px;
  }

  .chip {
    font-size: 10.5px;
    color: var(--grim-text-dim);
    background: var(--grim-surface-2);
    border-radius: 4px;
    padding: 2px 6px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
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
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 20px;
    margin-top: 24px;
  }

  .skeleton-card {
    aspect-ratio: 2 / 3;
    border-radius: 8px;
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

    .shelf {
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 14px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-card {
      animation: none;
    }

    .cover,
    .read-badge {
      transition: none;
    }

    .card:not(.locked):hover .cover {
      transform: none;
    }
  }
</style>
