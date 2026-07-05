<script>
  // Continuous book reader: renders a flat, unbroken run of chunks (no
  // per-chunk cards — chunk boundaries are invisible) with a section heading
  // whenever section_path changes, and infinite-scrolls further /read pages
  // via IntersectionObserver. The first page arrives pre-signed as a prop from
  // the host route's +page.server.js load(); every later page is fetched from
  // the sibling read/+server.js proxy, which signs images the same way.
  //
  // Deliberately brutalist-styled with the --grimb-* tokens (theme.css), a
  // second palette used ONLY here and in the sticky bar's Chapters dropdown
  // (ChaptersNav.svelte): the rest of the grimoire tree (entities, stat
  // blocks) keeps the oxblood theme untouched.
  import { apiFetch } from "$lib/grimoire/api.js";
  import { renderChunk } from "$lib/grimoire/renderChunk.js";
  import ChaptersNav from "$lib/grimoire/ChaptersNav.svelte";

  let {
    campaignId,
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

  // No prop-change reset effect: the host page wraps this component in
  // `{#key bookId+fromCursor}`, so a different book or a new `from` deep-link
  // cursor remounts the whole component (fresh $state, observers reconnected)
  // rather than mutating props under an existing instance. Infinite scroll
  // only ever appends to local `items`/`nextCursor` state below.

  // Book display name + total chunk count for the sticky bar's "seq/total".
  // Owns its own fetch (same rationale as ChaptersNav's /sections fetch
  // below: neither host has to).
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

  // A short label for a section: the last path segment (mirrors the
  // backend's list_sections `title`, so the TOC and reader agree).
  function sectionTitle(sectionPath) {
    if (!sectionPath) return "(no section)";
    const last = sectionPath.split("/").pop().trim();
    return last || sectionPath;
  }

  // The section's immediate parent segment, for the eyebrow chip — only
  // present when the section_path nests more than one level deep.
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

  // Ingest prepends the section name as a title line to every chunk's content
  // (marker.py flush(), so the entity extractor can name the monster from text
  // alone). The reader already shows that name as the section <h2> and in the
  // sticky bar, so a leading block that merely repeats it renders the title
  // twice. Drop it, comparing case-insensitively against both the full
  // section_path and its last "/" segment (the visible title).
  function bodyBlocks(item) {
    const blocks = renderChunk(item.content);
    const first = blocks[0];
    if (!first || (first.type !== "heading" && first.type !== "para")) return blocks;
    const norm = (s) => (s ?? "").trim().toUpperCase();
    const head = norm(first.text);
    if (head && (head === norm(item.section_path) || head === norm(sectionTitle(item.section_path)))) {
      return blocks.slice(1);
    }
    return blocks;
  }

  async function loadMore() {
    if (loadingMore || !nextCursor) return;
    loadingMore = true;
    loadError = "";
    try {
      const qs = new URLSearchParams({ cursor: nextCursor, limit: "40" });
      const res = await fetch(
        `/app/grimoire/${encodeURIComponent(campaignId)}/book/${encodeURIComponent(bookId)}/read?${qs}`,
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

  // Infinite scroll: the default (viewport) root is correct here even though
  // the actual scrolling ancestor is `.pane-read`/`.frame` (not the window —
  // the app root is height:100dvh/overflow:hidden so the window itself never
  // scrolls), because getBoundingClientRect-based intersection still reflects
  // the ancestor's scroll offset regardless of which element owns the
  // scrollbar.
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

  // Scrollspy for the sticky bar's "current section": whichever section
  // heading most recently crossed into the top 20% band becomes active and
  // stays active until a later heading crosses. Re-attaches whenever the
  // rendered item set changes (a fresh navigation, or a page appended).
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

  // Deep-link anchor: scroll the target chunk into view once it is rendered.
  // Re-tries as more pages load (in case the redirect's cursor landed the
  // anchor just outside the first page, e.g. the null-seq edge case).
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

<!-- The public tier's root layout already loads these two families globally;
     the private app doesn't, so load them here, scoped to wherever the
     reader actually mounts. ChaptersNav.svelte also uses --grimb-mono but is
     always a child of this component, so it never needs its own copy of
     this block. -->
<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link
    href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<div class="grimb-reader">
  <div class="grimb-bar">
    <span class="grimb-bar-title">{bookMeta.displayName}</span>
    <div class="grimb-bar-right">
      <span class="grimb-bar-pos">
        {sectionTitle(activeSectionPath).toUpperCase()}
        {#if activeSeq != null && bookMeta.chunkCount}
          · {activeSeq + 1}/{bookMeta.chunkCount}
        {/if}
      </span>
      <ChaptersNav {bookId} {activeSectionPath} />
    </div>
  </div>

  <div class="grimb-panel" bind:this={containerEl}>
    {#each rows as row, i (row.item.id)}
      {#if row.showHeading}
        {#if i > 0}
          <div class="grimb-divider" aria-hidden="true">
            <span class="grimb-divider-line"></span>
            <span class="grimb-divider-mark">§</span>
            <span class="grimb-divider-line"></span>
          </div>
        {/if}
        <div
          class="grimb-heading"
          data-heading
          data-seq={row.item.seq}
          data-section={row.item.section_path ?? ""}
        >
          {#if sectionParent(row.item.section_path)}
            <span class="grimb-chip">{sectionParent(row.item.section_path)}</span>
          {/if}
          <h2 class="grimb-section-title">{sectionTitle(row.item.section_path)}</h2>
        </div>
      {/if}

      <div class="grimb-chunk" id="c-{row.item.id}">
        <a
          class="grimb-anchor"
          href="#c-{row.item.id}"
          aria-label="Link to this passage"
        >
          #
        </a>
        {#if row.item.kind === "image"}
          <figure class="grimb-figure">
            {#if row.item.image_url}
              <img
                class="grimb-image"
                src={row.item.image_url}
                alt={row.item.content || "sourcebook illustration"}
              />
            {/if}
            {#if row.item.content}
              <figcaption class="grimb-caption">{row.item.content}</figcaption>
            {/if}
          </figure>
        {:else}
          {#each bodyBlocks(row.item) as block, bi (bi)}
            {#if block.type === "heading"}
              <h3 class="grimb-inline-heading">{block.text}</h3>
            {:else if block.type === "list"}
              <ul class="grimb-list">
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

    <div class="grimb-sentinel" bind:this={sentinelEl}>
      {#if loadingMore}
        <p class="grimb-status">Loading…</p>
      {:else if loadError}
        <button class="grimb-retry" onclick={loadMore}>Retry</button>
      {:else if !nextCursor}
        <p class="grimb-status">— end of book —</p>
      {/if}
    </div>
  </div>
</div>

<style>
  .grimb-reader {
    min-height: 100%;
    background: var(--grimb-cream);
    color: var(--grimb-ink);
  }

  .grimb-bar {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1.25rem;
    background: var(--grimb-cream);
    border-bottom: var(--grimb-border);
    font-family: var(--grimb-mono);
    font-size: 0.7rem;
    letter-spacing: 0.05em;
  }

  .grimb-bar-title {
    text-transform: uppercase;
    font-weight: 700;
  }

  .grimb-bar-right {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    min-width: 0;
  }

  .grimb-bar-pos {
    text-transform: uppercase;
    color: var(--grimb-ink-3);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .grimb-panel {
    max-width: 60rem;
    margin: 0 auto;
    padding: clamp(1.5rem, 5vw, 3rem) clamp(1.25rem, 6vw, 3rem);
    background: var(--grimb-paper);
    border: var(--grimb-border);
    border-top: none;
  }

  .grimb-divider {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin: 2.5rem 0;
    max-width: 72ch;
    margin-inline: auto;
  }

  .grimb-divider-line {
    flex: 1;
    height: 2px;
    background: var(--grimb-ink);
  }

  .grimb-divider-mark {
    font-family: var(--grimb-mono);
    font-size: 0.85rem;
    color: var(--grimb-ink-3);
  }

  .grimb-heading {
    max-width: 72ch;
    margin: 0 auto 1.25rem;
  }

  .grimb-chip {
    display: inline-flex;
    align-items: center;
    min-height: 1.5rem;
    padding: 0.1rem 0.5rem;
    margin-bottom: 0.6rem;
    font-family: var(--grimb-mono);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    background: var(--grimb-yellow);
    color: var(--grimb-ink);
    border: 1px solid var(--grimb-ink);
  }

  .grimb-section-title {
    font-family: var(--grimb-serif);
    font-weight: 400;
    font-size: 2rem;
    line-height: 1.1;
    color: var(--grimb-ink);
  }

  .grimb-chunk {
    position: relative;
    max-width: 72ch;
    margin: 0 auto;
    font-family: var(--grimb-serif);
    font-size: 1.13rem;
    line-height: 1.66;
    color: var(--grimb-ink);
  }

  .grimb-chunk p {
    margin: 0 0 1rem;
  }

  .grimb-inline-heading {
    font-family: var(--grimb-mono);
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--grimb-ink);
    margin: 1.5rem 0 0.6rem;
  }

  .grimb-inline-heading:first-child {
    margin-top: 0;
  }

  .grimb-list {
    margin: 0 0 1rem 1.3rem;
    padding: 0;
    list-style: disc;
  }

  .grimb-list li {
    margin-bottom: 0.4rem;
  }

  /* Hover-only anchor: sits in the left margin of the chunk block, invisible
   * until the block is hovered (or the link itself is focused via keyboard). */
  .grimb-anchor {
    position: absolute;
    left: -1.75rem;
    top: 0.2rem;
    font-family: var(--grimb-mono);
    font-size: 0.9rem;
    color: var(--grimb-ink-3);
    text-decoration: none;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  .grimb-chunk:hover .grimb-anchor,
  .grimb-anchor:focus-visible {
    opacity: 1;
  }

  .grimb-figure {
    margin: 1.5rem 0;
  }

  .grimb-image {
    width: 100%;
    height: auto;
    border: var(--grimb-border);
  }

  .grimb-caption {
    margin-top: 0.6rem;
    font-family: var(--grimb-serif);
    font-style: italic;
    font-size: 0.95rem;
    color: var(--grimb-ink-3);
    text-align: center;
  }

  .grimb-sentinel {
    display: flex;
    justify-content: center;
    padding: 2rem 0 0.5rem;
  }

  .grimb-status {
    font-family: var(--grimb-mono);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--grimb-ink-3);
  }

  .grimb-retry {
    font-family: var(--grimb-mono);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    min-height: 2.5rem;
    padding: 0.5rem 1rem;
    background: var(--grimb-paper);
    color: var(--grimb-ink);
    border: var(--grimb-border);
    cursor: pointer;
  }

  @media (max-width: 640px) {
    .grimb-anchor {
      left: 0.25rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .grimb-anchor {
      transition: none;
    }
  }
</style>
