<script>
  // Continuous book reader for the public tier. Mirrors the private tier's
  // Reader.svelte (see that file's docblock for the full design rationale:
  // flat, boundary-less chunk runs, section headings on section_path change,
  // infinite scroll via IntersectionObserver, no entity chips). Reuses the
  // public design-system tokens directly (--cream/--paper/--ink/--accent/
  // --mono/--serif already ARE the brutalist palette the private reader had
  // to introduce new tokens to match), so no new CSS variables are needed
  // here — just flat 2px ink borders, never a box-shadow.
  import { apiFetch } from "$lib/public/grimoire/api.js";
  import { renderChunk } from "$lib/public/grimoire/renderChunk.js";

  let {
    bookId,
    items: initialItems,
    nextCursor: initialNextCursor,
    anchorChunkId = null,
  } = $props();

  let items = $state(initialItems);
  let nextCursor = $state(initialNextCursor);
  let loadingMore = $state(false);
  let loadError = $state("");
  let scrolledToAnchor = $state(false);

  let bookMeta = $state({ displayName: bookId, chunkCount: null });
  let activeSeq = $state(initialItems[0]?.seq ?? null);
  let activeSectionPath = $state(initialItems[0]?.section_path ?? null);

  let containerEl;
  let sentinelEl;

  // No prop-change reset effect here either: the host page wraps this in
  // `{#key bookId+fromCursor}`, so a fresh navigation remounts the component.

  $effect(() => {
    loadBookMeta(bookId);
  });

  async function loadBookMeta(id) {
    try {
      const books = await apiFetch("/books");
      const book = books.find((b) => b.book_id === id);
      bookMeta = {
        displayName: book?.display_name ?? id,
        chunkCount: book?.chunk_count ?? null,
      };
    } catch {
      bookMeta = { displayName: id, chunkCount: null };
    }
  }

  function sectionTitle(sectionPath) {
    if (!sectionPath) return "(no section)";
    const last = sectionPath.split("/").pop().trim();
    return last || sectionPath;
  }

  function sectionParent(sectionPath) {
    if (!sectionPath) return null;
    const parts = sectionPath
      .split("/")
      .map((p) => p.trim())
      .filter(Boolean);
    return parts.length >= 2 ? parts[parts.length - 2] : null;
  }

  const rows = $derived(
    items.map((item, i) => ({
      item,
      showHeading: i === 0 || item.section_path !== items[i - 1].section_path,
    })),
  );

  async function loadMore() {
    if (loadingMore || !nextCursor) return;
    loadingMore = true;
    loadError = "";
    try {
      const qs = new URLSearchParams({ cursor: nextCursor, limit: "40" });
      const res = await fetch(
        `/app/grimoire/book/${encodeURIComponent(bookId)}/read?${qs}`,
        { signal: AbortSignal.timeout(15_000) },
      );
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const page = await res.json();
      items = [...items, ...page.items];
      nextCursor = page.next_cursor;
    } catch (e) {
      loadError = e.message || "failed to load more";
    } finally {
      loadingMore = false;
    }
  }

  // See the private Reader's docblock: the default (viewport) root is
  // correct even though the actual scrolling element is an ancestor, because
  // the app root never scrolls the window itself.
  $effect(() => {
    if (!sentinelEl) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadMore();
      },
      { rootMargin: "600px 0px" },
    );
    io.observe(sentinelEl);
    return () => io.disconnect();
  });

  $effect(() => {
    void items.length;
    if (!containerEl) return;
    const headings = [...containerEl.querySelectorAll("[data-heading]")];
    if (!headings.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const hit = entries
          .filter((e) => e.isIntersecting)
          .sort(
            (a, b) => Number(b.target.dataset.seq) - Number(a.target.dataset.seq),
          )[0];
        if (hit) {
          activeSeq = Number(hit.target.dataset.seq);
          activeSectionPath = hit.target.dataset.section || null;
        }
      },
      { rootMargin: "0px 0px -80% 0px", threshold: 0 },
    );
    headings.forEach((h) => observer.observe(h));
    return () => observer.disconnect();
  });

  $effect(() => {
    void items.length;
    if (scrolledToAnchor || !anchorChunkId || !containerEl) return;
    let el;
    try {
      el = containerEl.querySelector(`#c-${CSS.escape(anchorChunkId)}`);
    } catch {
      el = null;
    }
    if (!el) return;
    scrolledToAnchor = true;
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  });
</script>

<div class="pub-reader">
  <div class="pub-bar mono">
    <span class="pub-bar-title">{bookMeta.displayName}</span>
    <span class="pub-bar-pos">
      {sectionTitle(activeSectionPath).toUpperCase()}
      {#if activeSeq != null && bookMeta.chunkCount}
        · {activeSeq + 1}/{bookMeta.chunkCount}
      {/if}
    </span>
  </div>

  <div class="pub-panel" bind:this={containerEl}>
    {#each rows as row, i (row.item.id)}
      {#if row.showHeading}
        {#if i > 0}
          <div class="pub-divider" aria-hidden="true">
            <span class="pub-divider-line"></span>
            <span class="pub-divider-mark mono">§</span>
            <span class="pub-divider-line"></span>
          </div>
        {/if}
        <div
          class="pub-heading"
          data-heading
          data-seq={row.item.seq}
          data-section={row.item.section_path ?? ""}
        >
          {#if sectionParent(row.item.section_path)}
            <span class="pub-chip mono">{sectionParent(row.item.section_path)}</span>
          {/if}
          <h2 class="display pub-section-title">{sectionTitle(row.item.section_path)}</h2>
        </div>
      {/if}

      <div class="pub-chunk" id="c-{row.item.id}">
        <a
          class="pub-anchor mono"
          href="#c-{row.item.id}"
          aria-label="Link to this passage"
        >
          #
        </a>
        {#if row.item.kind === "image"}
          <figure class="pub-figure">
            {#if row.item.image_url}
              <img
                class="pub-image"
                src={row.item.image_url}
                alt={row.item.content || "sourcebook illustration"}
              />
            {/if}
            {#if row.item.content}
              <figcaption class="pub-caption">{row.item.content}</figcaption>
            {/if}
          </figure>
        {:else}
          {#each renderChunk(row.item.content) as block, bi (bi)}
            {#if block.type === "heading"}
              <h3 class="eyebrow pub-inline-heading">{block.text}</h3>
            {:else if block.type === "list"}
              <ul class="pub-list">
                {#each block.items as li, lii (lii)}
                  <li>{li}</li>
                {/each}
              </ul>
            {:else}
              <p>{block.text}</p>
            {/if}
          {/each}
        {/if}
      </div>
    {/each}

    <div class="pub-sentinel" bind:this={sentinelEl}>
      {#if loadingMore}
        <p class="mono pub-status">Loading…</p>
      {:else if loadError}
        <button class="mono pub-retry" onclick={loadMore}>Retry</button>
      {:else if !nextCursor}
        <p class="mono pub-status">— end of book —</p>
      {/if}
    </div>
  </div>
</div>

<style>
  .pub-reader {
    min-height: 60vh;
    background: var(--cream);
    color: var(--ink);
  }

  .pub-bar {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    padding: 12px 20px;
    background: var(--cream);
    border-bottom: 2px solid var(--ink);
    font-size: 11px;
    letter-spacing: 0.08em;
  }

  .pub-bar-title {
    text-transform: uppercase;
    font-weight: 700;
  }

  .pub-bar-pos {
    text-transform: uppercase;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pub-panel {
    max-width: 800px;
    margin: 0 auto;
    padding: clamp(24px, 5vw, 48px) clamp(20px, 6vw, 48px);
    background: var(--paper);
    border: 2px solid var(--ink);
    border-top: none;
  }

  .pub-divider {
    display: flex;
    align-items: center;
    gap: 16px;
    margin: 40px 0;
    max-width: 66ch;
    margin-inline: auto;
  }

  .pub-divider-line {
    flex: 1;
    height: 2px;
    background: var(--ink);
  }

  .pub-divider-mark {
    font-size: 14px;
    color: var(--ink-3);
  }

  .pub-heading {
    max-width: 66ch;
    margin: 0 auto 20px;
  }

  .pub-chip {
    display: inline-flex;
    align-items: center;
    min-height: 24px;
    padding: 2px 8px;
    margin-bottom: 10px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    background: var(--accent);
    color: var(--ink);
    border: 1px solid var(--ink);
  }

  .pub-section-title {
    font-size: 32px;
    color: var(--ink);
  }

  .pub-chunk {
    position: relative;
    max-width: 66ch;
    margin: 0 auto;
    font-family: var(--sans);
    font-size: 18px;
    line-height: 1.66;
    color: var(--ink);
  }

  .pub-chunk p {
    margin: 0 0 16px;
  }

  .pub-inline-heading {
    margin: 24px 0 10px;
    color: var(--ink);
  }

  .pub-inline-heading:first-child {
    margin-top: 0;
  }

  .pub-list {
    margin: 0 0 16px 22px;
    padding: 0;
    list-style: disc;
  }

  .pub-list li {
    margin-bottom: 6px;
  }

  .pub-anchor {
    position: absolute;
    left: -28px;
    top: 3px;
    font-size: 14px;
    color: var(--ink-3);
    text-decoration: none;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  .pub-chunk:hover .pub-anchor,
  .pub-anchor:focus-visible {
    opacity: 1;
  }

  .pub-figure {
    margin: 24px 0;
  }

  .pub-image {
    width: 100%;
    height: auto;
    border: 2px solid var(--ink);
  }

  .pub-caption {
    margin-top: 10px;
    font-family: var(--sans);
    font-style: italic;
    font-size: 15px;
    color: var(--ink-3);
    text-align: center;
  }

  .pub-sentinel {
    display: flex;
    justify-content: center;
    padding: 32px 0 8px;
  }

  .pub-status {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-3);
  }

  .pub-retry {
    min-height: 40px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    background: var(--paper);
    color: var(--ink);
    /* Flat 2px ink border only, no shadow: grimoire never uses box-shadow. */
    border: 2px solid var(--ink);
    cursor: pointer;
  }

  .pub-retry:hover {
    color: var(--ink);
    background: var(--bg-elev);
  }

  @media (max-width: 640px) {
    .pub-anchor {
      left: 4px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .pub-anchor {
      transition: none;
    }
  }
</style>
