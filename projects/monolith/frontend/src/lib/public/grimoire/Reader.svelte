<script>
  // Continuous book reader for the public tier. Mirrors the private tier's
  // Reader.svelte (see that file's docblock for the full design rationale:
  // flat, boundary-less chunk runs, section headings on section_path change,
  // infinite scroll via IntersectionObserver, no entity chips).
  //
  // Styled with the clean grimoire theme (--grim-* tokens from
  // $lib/grimoire/theme.css, which resolve here via the ancestor
  // `.grimoire` wrapper), not the site-wide brutalist design-system tokens
  // this reader used before this pass: a cool-paper reading surface, serif
  // body copy, hairline rules, small-caps section labels, no hard ink
  // borders or mono chrome.
  //
  // Layout: a left section-hierarchy sidebar (desktop, >760px) alongside the
  // reading column, per docs/plans/assets/2026-07-05-grimoire-reskin-
  // mockup.html's Reader tab. The sidebar is ChaptersNav in its new
  // `variant="sidebar"` mode: the same fetch + active-section-highlight
  // logic as the original Chapters dropdown, just rendered inline and always
  // expanded instead of behind a toggle. Below 760px the sidebar is hidden
  // and the original dropdown affordance (`variant="dropdown"`) reappears in
  // the sticky bar as the compact mobile chapters entry point.
  import { apiFetch } from "$lib/public/grimoire/api.js";
  import { renderChunk } from "$lib/public/grimoire/renderChunk.js";
  import ChaptersNav from "$lib/public/grimoire/ChaptersNav.svelte";

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
  let scrolledToSectionAnchor = $state(false);

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

  // Deep-link slug for a section heading, e.g. "Chapter 1 / Into the Mists"
  // -> "chapter-1-into-the-mists". Callers prefix with "s-" (see the
  // `.pub-heading` id and anchor below) so section ids can never collide
  // with a chunk's "c-{id}" id in the same DOM.
  function sectionSlug(sectionPath) {
    return (sectionPath ?? "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
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

  // Section-heading deep links. The host route (+page.svelte) only parses
  // "#c-" hashes into the anchorChunkId prop above (the chunk case), so a
  // "#s-" section link is left to the browser's own hash scroll -- which
  // only fires on the initial hard navigation. A same-document hash change
  // (or a hash present before this component's headings exist, e.g. while
  // an earlier page of items is still loading) needs the same manual
  // scrollIntoView the chunk case gets, so read the hash directly here
  // rather than threading a second prop through the route.
  $effect(() => {
    void items.length;
    if (scrolledToSectionAnchor || !containerEl) return;
    const hash = typeof window !== "undefined" ? window.location.hash : "";
    if (!hash.startsWith("#s-")) return;
    let el;
    try {
      el = containerEl.querySelector(`#${CSS.escape(hash.slice(1))}`);
    } catch {
      el = null;
    }
    if (!el) return;
    scrolledToSectionAnchor = true;
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  });
</script>

<div class="pub-reader">
  <div class="pub-bar">
    <span class="pub-bar-title">{bookMeta.displayName}</span>
    <div class="pub-bar-right">
      <span class="pub-bar-pos">
        {sectionTitle(activeSectionPath)}
        {#if activeSeq != null && bookMeta.chunkCount}
          · {activeSeq + 1}/{bookMeta.chunkCount}
        {/if}
      </span>
      <!-- Compact mobile-only affordance: the sidebar below is hidden under
           760px, so the dropdown Chapters button is the only way to jump
           sections on a phone. Hidden on desktop via CSS (the sidebar
           replaces it there); still mounts and fetches regardless of
           viewport, same as the sidebar instance below -- a second cheap GET
           of /books/{bookId}/sections, not worth a JS media-query gate. -->
      <div class="pub-bar-chapters">
        <ChaptersNav {bookId} {activeSectionPath} variant="dropdown" />
      </div>
    </div>
  </div>

  <div class="pub-layout">
    <aside class="pub-toc" aria-label="Sections">
      <p class="pub-toc-label">{bookMeta.displayName}</p>
      <ChaptersNav {bookId} {activeSectionPath} variant="sidebar" />
    </aside>

    <div class="pub-panel" bind:this={containerEl}>
      {#each rows as row, i (row.item.id)}
        {#if row.showHeading}
          {#if i > 0}
            <hr class="pub-rule" aria-hidden="true" />
          {/if}
          <div
            class="pub-heading"
            id={"s-" + sectionSlug(row.item.section_path)}
            data-heading
            data-seq={row.item.seq}
            data-section={row.item.section_path ?? ""}
          >
            <a
              class="pub-anchor"
              href={"#s-" + sectionSlug(row.item.section_path)}
              aria-label="Link to this section"
            >
              #
            </a>
            {#if sectionParent(row.item.section_path)}
              <p class="pub-section-label grim-smallcaps">
                {sectionParent(row.item.section_path)}
              </p>
            {/if}
            <h2 class="pub-section-title">{sectionTitle(row.item.section_path)}</h2>
          </div>
        {/if}

        <div class="pub-chunk" id="c-{row.item.id}">
          <a
            class="pub-anchor"
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
            {#each bodyBlocks(row.item) as block, bi (bi)}
              {#if block.type === "heading"}
                <h3 class="pub-inline-heading">{block.text}</h3>
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
          <p class="pub-status">Loading…</p>
        {:else if loadError}
          <button class="pub-retry" onclick={loadMore}>Retry</button>
        {:else if !nextCursor}
          <p class="pub-status">— end of book —</p>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .pub-reader {
    min-height: 60vh;
    background: var(--grim-paper);
    color: var(--grim-ink);
  }

  .pub-bar {
    position: sticky;
    /* 58px is the app-shell topbar's own height (see the grimoire
       +layout.svelte `.topbar`); both bars are sticky at once, so this one
       has to park below it rather than at top:0 or they'd overlap. */
    top: 58px;
    z-index: 5;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 20px;
    background: color-mix(in srgb, var(--grim-paper) 90%, transparent);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--grim-line);
    font-size: 11px;
    letter-spacing: 0.12em;
  }

  .pub-bar-title {
    text-transform: uppercase;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .pub-bar-right {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  .pub-bar-pos {
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Sidebar covers this on desktop; only the mobile breakpoint below
     switches it back on. */
  .pub-bar-chapters {
    display: none;
  }

  .pub-layout {
    max-width: 1080px;
    margin: 0 auto;
    padding: clamp(24px, 5vw, 48px) clamp(20px, 6vw, 48px) 80px;
    display: grid;
    grid-template-columns: 244px 1fr;
    gap: 44px;
    align-items: start;
  }

  .pub-toc {
    position: sticky;
    /* Approx: 58px app topbar + this reader's own ~42px sticky bar. */
    top: 100px;
    max-height: calc(100vh - 140px);
    overflow-y: auto;
    border-right: 1px solid var(--grim-line-soft);
    padding-right: 20px;
  }

  .pub-toc-label {
    margin: 0 0 14px;
    font-family: var(--grim-serif);
    font-weight: 700;
    font-size: 15px;
    line-height: 1.25;
    color: var(--grim-ink);
    overflow-wrap: break-word;
  }

  .pub-panel {
    min-width: 0;
  }

  .pub-rule {
    max-width: 68ch;
    margin: 28px 0;
    height: 1px;
    border: 0;
    background: var(--grim-line);
  }

  .pub-heading {
    position: relative;
    max-width: 68ch;
    margin: 0 0 18px;
  }

  .pub-heading:hover .pub-anchor {
    opacity: 1;
  }

  .pub-section-label {
    margin: 0 0 6px;
    font-size: 13px;
    font-weight: 700;
    color: var(--grim-accent);
  }

  .pub-section-title {
    margin: 0;
    font-family: var(--grim-serif);
    font-size: 30px;
    font-weight: 600;
    letter-spacing: -0.01em;
    line-height: 1.15;
    color: var(--grim-ink);
  }

  .pub-chunk {
    position: relative;
    max-width: 68ch;
    font-family: var(--grim-serif);
    font-size: 17px;
    line-height: 1.66;
    color: var(--grim-ink);
  }

  .pub-chunk p {
    margin: 0 0 14px;
  }

  .pub-inline-heading {
    margin: 26px 0 8px;
    font-family: var(--grim-serif);
    font-size: 18px;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .pub-inline-heading:first-child {
    margin-top: 0;
  }

  .pub-list {
    margin: 0 0 14px 22px;
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
    font-size: 13px;
    color: var(--grim-text-faint);
    text-decoration: none;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  .pub-chunk:hover .pub-anchor,
  .pub-anchor:focus-visible {
    opacity: 1;
  }

  .pub-anchor:hover {
    color: var(--grim-accent);
  }

  .pub-figure {
    margin: 24px 0;
    max-width: 68ch;
  }

  /* Never upscale: max-width (not width) + no forced height keep small
     illustrations at their natural size, while max-height caps oversized
     scans so they never dominate the reading column. */
  .pub-image {
    display: block;
    max-width: 100%;
    height: auto;
    max-height: 60vh;
    margin-inline: auto;
    border: 1px solid var(--grim-line);
    border-radius: 8px;
  }

  .pub-caption {
    margin-top: 10px;
    font-family: var(--grim-serif);
    font-style: italic;
    font-size: 14px;
    color: var(--grim-text-dim);
    text-align: center;
  }

  .pub-sentinel {
    display: flex;
    justify-content: center;
    padding: 32px 0 8px;
    max-width: 68ch;
  }

  .pub-status {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--grim-text-faint);
  }

  .pub-retry {
    min-height: 40px;
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    background: var(--grim-surface);
    color: var(--grim-ink);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    cursor: pointer;
  }

  .pub-retry:hover {
    color: var(--grim-accent);
    border-color: var(--grim-accent);
  }

  @media (max-width: 760px) {
    .pub-layout {
      grid-template-columns: 1fr;
    }

    .pub-toc {
      display: none;
    }

    .pub-bar-chapters {
      display: inline-flex;
    }
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
