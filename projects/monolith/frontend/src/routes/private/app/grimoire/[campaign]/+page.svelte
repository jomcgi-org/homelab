<script>
  import { onMount, getContext } from "svelte";
  import { goto } from "$app/navigation";
  import {
    apiFetch,
    bookHref,
    entitiesHref,
    bookLastSeen,
    markBookSeen,
  } from "$lib/grimoire/api.js";

  // Campaign + viewpoint come from the layout context (route param + ?as=).
  const ctx = getContext("grimoire");

  let books = $state([]);
  let loading = $state(true);
  let error = $state("");

  let renamingId = $state("");
  let renameValue = $state("");

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

  onMount(load);

  function isNew(book) {
    if (!book.latest_chunk_at) return false;
    const seen = bookLastSeen(book.book_id);
    return !seen || book.latest_chunk_at > seen;
  }

  function openBook(book) {
    markBookSeen(book.book_id, book.latest_chunk_at);
    goto(bookHref(ctx.campaignId, book.book_id, ctx.viewpoint));
  }

  function coverage(book) {
    if (!book.chunk_count) return 0;
    return Math.round((book.extracted_count / book.chunk_count) * 100);
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

  function startRename(book) {
    renamingId = book.book_id;
    renameValue = book.display_name;
  }

  async function saveRename(book) {
    const name = renameValue.trim();
    renamingId = "";
    if (!name || name === book.display_name) return;
    try {
      await apiFetch(`/books/${encodeURIComponent(book.book_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ display_name: name }),
      });
      book.display_name = name;
      books = [...books];
    } catch (e) {
      error = e.message;
    }
  }
</script>

<div class="library grim-paper">
  <div class="lib-head">
    <h1 class="grim-title lib-title">Library</h1>
    <a class="entities-link" href={entitiesHref(ctx.campaignId, ctx.viewpoint)}>
      Browse entities →
    </a>
  </div>

  {#if loading}
    <div class="skeletons">
      {#each [1, 2, 3] as n (n)}
        <div class="skeleton"></div>
      {/each}
    </div>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else if books.length === 0}
    <div class="empty">
      <p class="empty-lead">No books loaded yet.</p>
      <p class="empty-help">
        Load a sourcebook with <code>tools/upload-book.sh</code>; coverage will
        appear here and tick up as extraction runs.
      </p>
    </div>
  {:else}
    <ul class="books">
      {#each books as book (book.book_id)}
        <li class="book">
          <div class="book-main">
            <div class="book-name-row">
              {#if renamingId === book.book_id}
                <!-- svelte-ignore a11y_autofocus -->
                <input
                  class="rename-input"
                  bind:value={renameValue}
                  autofocus
                  onblur={() => saveRename(book)}
                  onkeydown={(e) => {
                    if (e.key === "Enter") saveRename(book);
                    if (e.key === "Escape") (renamingId = "");
                  }}
                />
              {:else}
                <button class="book-name grim-title" onclick={() => openBook(book)}>
                  {book.display_name}
                </button>
                {#if isNew(book)}
                  <span class="new-badge">new</span>
                {/if}
                <button
                  class="rename-btn"
                  title="Rename book"
                  aria-label="Rename book"
                  onclick={() => startRename(book)}>✎</button
                >
              {/if}
            </div>

            <div class="book-stats grim-stagger">
              <span class="stat">
                <strong>{book.extracted_count}</strong> / {book.chunk_count} extracted
              </span>
              <span class="stat">{book.image_count} images</span>
              <span class="stat">{book.entity_count} entities</span>
              <span class="stat muted">loaded {timeAgo(book.last_loaded_at)}</span>
            </div>

            <div
              class="coverage"
              role="progressbar"
              aria-valuenow={coverage(book)}
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <div class="coverage-fill" style="width:{coverage(book)}%"></div>
            </div>
          </div>

          <button class="book-open" onclick={() => openBook(book)}>Read →</button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .library {
    min-height: 100%;
    padding: clamp(1rem, 4vw, 2.5rem);
    max-width: 60rem;
    margin: 0 auto;
  }

  .lib-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.5rem;
  }

  .lib-title {
    font-size: clamp(1.5rem, 5vw, 2.25rem);
    color: var(--grim-accent);
  }

  .entities-link {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-accent);
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
  }

  .books {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .book {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 1rem 1.15rem;
    border: 1px solid var(--grim-paper-line);
    background: var(--bg);
  }

  .book-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .book-name-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .book-name {
    font-size: 1.2rem;
    color: var(--fg);
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    text-align: left;
  }

  .book-name:hover {
    color: var(--grim-accent);
  }

  .new-badge {
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 0.1rem 0.35rem;
    background: var(--grim-accent);
    color: #fff;
    font-family: var(--font-mono);
  }

  .rename-btn {
    background: none;
    border: none;
    color: var(--fg-tertiary);
    cursor: pointer;
    font-size: 0.8rem;
    min-height: 2rem;
    min-width: 2rem;
  }

  .rename-btn:hover {
    color: var(--grim-accent);
  }

  .rename-input {
    font-family: var(--grim-serif);
    font-size: 1.1rem;
    padding: 0.25rem 0.4rem;
    border: var(--border-thin);
    background: var(--bg);
    color: var(--fg);
    flex: 1;
  }

  .book-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    font-size: 0.75rem;
    color: var(--fg-secondary);
  }

  .stat strong {
    color: var(--grim-accent);
    font-variant-numeric: tabular-nums;
  }

  .stat.muted {
    color: var(--fg-tertiary);
  }

  .coverage {
    height: 4px;
    background: var(--grim-paper-line);
    overflow: hidden;
  }

  .coverage-fill {
    height: 100%;
    background: var(--grim-accent);
    transition: width 0.4s ease;
  }

  .book-open {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    min-height: 2.5rem;
    padding: 0.5rem 0.9rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .book-open:hover {
    border-color: var(--grim-accent);
    color: var(--grim-accent);
  }

  .empty {
    padding: 3rem 1rem;
    text-align: center;
  }

  .empty-lead {
    font-family: var(--grim-serif);
    font-size: 1.3rem;
    margin-bottom: 0.5rem;
  }

  .empty-help {
    font-size: 0.82rem;
    color: var(--fg-secondary);
  }

  .empty-help code {
    background: var(--surface);
    padding: 0.1rem 0.3rem;
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .skeleton {
    height: 5.5rem;
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

  @media (min-width: 880px) {
    .book-stats {
      gap: 1.25rem;
    }
  }

  @media (max-width: 600px) {
    .book {
      flex-direction: column;
      align-items: stretch;
    }
    .book-open {
      align-self: flex-start;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton {
      animation: none;
    }
    .coverage-fill {
      transition: none;
    }
  }
</style>
