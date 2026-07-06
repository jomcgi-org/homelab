<script>
  // Continuous book reader: renders a flat, unbroken run of chunks (no
  // per-chunk cards — chunk boundaries are invisible) with a section heading
  // whenever section_path changes, and infinite-scrolls further /read pages
  // via IntersectionObserver. The first page arrives pre-signed as a prop from
  // the host route's +page.server.js load(); every later page is fetched from
  // the sibling read/+server.js proxy, which signs images the same way.
  //
  // Styled with the clean grimoire theme (--grim-* tokens from
  // $lib/grimoire/theme.css, which resolve here via the ancestor `.grimoire`
  // wrapper) rather than the old brutalist second palette (a separate set of
  // ink/cream/yellow custom properties) this reader used before this pass: a
  // cool-paper reading surface, serif body copy, hairline rules, small-caps
  // section labels, no hard ink borders or mono chrome. Mirrors the public
  // tier's Reader.svelte (see that file for
  // the shared design rationale); the differences here are all
  // private-only: campaignId-scoped routes, and DM/player viewpoint
  // (`?as=`) carried through ChaptersNav via the `grimoire` context rather
  // than a plain prop.
  //
  // Layout: a left section-hierarchy sidebar (desktop, >760px) alongside the
  // reading column, per docs/plans/assets/2026-07-05-grimoire-reskin-
  // mockup.html's Reader tab. The sidebar is ChaptersNav in its new
  // `variant="sidebar"` mode: the same fetch + active-section-highlight
  // logic as the original Chapters dropdown, just rendered inline and always
  // expanded instead of behind a toggle. Below 760px the sidebar is hidden
  // and the original dropdown affordance (`variant="dropdown"`) reappears in
  // the sticky bar as the compact mobile chapters entry point.
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

  // The section's immediate parent segment, for the eyebrow label — only
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

<div class="rdr-reader">
  <div class="rdr-bar">
    <span class="rdr-bar-title">{bookMeta.displayName}</span>
    <div class="rdr-bar-right">
      <span class="rdr-bar-pos">
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
      <div class="rdr-bar-chapters">
        <ChaptersNav {bookId} {activeSectionPath} variant="dropdown" />
      </div>
    </div>
  </div>

  <div class="rdr-layout">
    <aside class="rdr-toc" aria-label="Sections">
      <p class="rdr-toc-label">{bookMeta.displayName}</p>
      <ChaptersNav {bookId} {activeSectionPath} variant="sidebar" />
    </aside>

    <div class="rdr-panel" bind:this={containerEl}>
      {#each rows as row, i (row.item.id)}
        {#if row.showHeading}
          {#if i > 0}
            <hr class="rdr-rule" aria-hidden="true" />
          {/if}
          <div
            class="rdr-heading"
            data-heading
            data-seq={row.item.seq}
            data-section={row.item.section_path ?? ""}
          >
            {#if sectionParent(row.item.section_path)}
              <p class="rdr-section-label grim-smallcaps">
                {sectionParent(row.item.section_path)}
              </p>
            {/if}
            <h2 class="rdr-section-title">{sectionTitle(row.item.section_path)}</h2>
          </div>
        {/if}

        <div class="rdr-chunk" id="c-{row.item.id}">
          <a
            class="rdr-anchor"
            href="#c-{row.item.id}"
            aria-label="Link to this passage"
          >
            #
          </a>
          {#if row.item.kind === "image"}
            <figure class="rdr-figure">
              {#if row.item.image_url}
                <img
                  class="rdr-image"
                  src={row.item.image_url}
                  alt={row.item.content || "sourcebook illustration"}
                />
              {/if}
              {#if row.item.content}
                <figcaption class="rdr-caption">{row.item.content}</figcaption>
              {/if}
            </figure>
          {:else}
            {#each bodyBlocks(row.item) as block, bi (bi)}
              {#if block.type === "heading"}
                <h3 class="rdr-inline-heading">{block.text}</h3>
              {:else if block.type === "list"}
                <ul class="rdr-list">
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

      <div class="rdr-sentinel" bind:this={sentinelEl}>
        {#if loadingMore}
          <p class="rdr-status">Loading…</p>
        {:else if loadError}
          <button class="rdr-retry" onclick={loadMore}>Retry</button>
        {:else if !nextCursor}
          <p class="rdr-status">— end of book —</p>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .rdr-reader {
    min-height: 100%;
    background: var(--grim-paper);
    color: var(--grim-ink);
  }

  .rdr-bar {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 20px;
    background: color-mix(in srgb, var(--grim-paper) 90%, transparent);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--grim-line);
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.12em;
  }

  .rdr-bar-title {
    text-transform: uppercase;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .rdr-bar-right {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  .rdr-bar-pos {
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Sidebar covers this on desktop; only the mobile breakpoint below
     switches it back on. */
  .rdr-bar-chapters {
    display: none;
  }

  .rdr-layout {
    max-width: 1080px;
    margin: 0 auto;
    padding: clamp(24px, 5vw, 48px) clamp(20px, 6vw, 48px) 80px;
    display: grid;
    grid-template-columns: 244px 1fr;
    gap: 44px;
    align-items: start;
  }

  .rdr-toc {
    /* Sticky relative to .pane-read (this reader's scrolling ancestor, set
       up by Shell.svelte), not the window: the outer app shell's own topbar
       lives above .pane-read and isn't part of this scroll context, so only
       .rdr-bar's own height (~52px) needs to be cleared here. The max-height
       subtraction is a conservative estimate (rounds down) so the aside's
       own overflow-y:auto scrollbar always has room to appear rather than
       silently clipping sections off the bottom of the viewport. */
    position: sticky;
    top: 56px;
    max-height: calc(100dvh - 200px);
    overflow-y: auto;
    border-right: 1px solid var(--grim-line-soft);
    padding-right: 20px;
  }

  .rdr-toc-label {
    margin: 0 0 14px;
    font-family: var(--grim-serif);
    font-weight: 700;
    font-size: 15px;
    line-height: 1.25;
    color: var(--grim-ink);
    overflow-wrap: break-word;
  }

  .rdr-panel {
    min-width: 0;
  }

  .rdr-rule {
    max-width: 68ch;
    margin: 28px 0;
    height: 1px;
    border: 0;
    background: var(--grim-line);
  }

  .rdr-heading {
    max-width: 68ch;
    margin: 0 0 18px;
  }

  .rdr-section-label {
    margin: 0 0 6px;
    font-size: 13px;
    font-weight: 700;
    color: var(--grim-accent);
  }

  .rdr-section-title {
    margin: 0;
    font-family: var(--grim-serif);
    font-size: 30px;
    font-weight: 600;
    letter-spacing: -0.01em;
    line-height: 1.15;
    color: var(--grim-ink);
  }

  .rdr-chunk {
    position: relative;
    max-width: 68ch;
    font-family: var(--grim-serif);
    font-size: 17px;
    line-height: 1.66;
    color: var(--grim-ink);
  }

  .rdr-chunk p {
    margin: 0 0 14px;
  }

  .rdr-inline-heading {
    margin: 26px 0 8px;
    font-family: var(--grim-serif);
    font-size: 18px;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .rdr-inline-heading:first-child {
    margin-top: 0;
  }

  .rdr-list {
    margin: 0 0 14px 22px;
    padding: 0;
    list-style: disc;
  }

  .rdr-list li {
    margin-bottom: 6px;
  }

  .rdr-anchor {
    position: absolute;
    left: -28px;
    top: 3px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--grim-text-faint);
    text-decoration: none;
    opacity: 0;
    transition: opacity 120ms ease;
  }

  .rdr-chunk:hover .rdr-anchor,
  .rdr-anchor:focus-visible {
    opacity: 1;
  }

  .rdr-anchor:hover {
    color: var(--grim-accent);
  }

  .rdr-figure {
    margin: 24px 0;
    max-width: 68ch;
  }

  /* Never upscale: max-width (not width) + no forced height keep small
     illustrations at their natural size, while max-height caps oversized
     scans so they never dominate the reading column. */
  .rdr-image {
    display: block;
    max-width: 100%;
    height: auto;
    max-height: 60vh;
    margin-inline: auto;
    border: 1px solid var(--grim-line);
    border-radius: 8px;
  }

  .rdr-caption {
    margin-top: 10px;
    font-family: var(--grim-serif);
    font-style: italic;
    font-size: 14px;
    color: var(--grim-text-dim);
    text-align: center;
  }

  .rdr-sentinel {
    display: flex;
    justify-content: center;
    padding: 32px 0 8px;
    max-width: 68ch;
  }

  .rdr-status {
    font-family: var(--font-mono);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--grim-text-faint);
  }

  .rdr-retry {
    font-family: var(--font-mono);
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

  .rdr-retry:hover {
    color: var(--grim-accent);
    border-color: var(--grim-accent);
  }

  @media (max-width: 760px) {
    .rdr-layout {
      grid-template-columns: 1fr;
    }

    .rdr-toc {
      display: none;
    }

    .rdr-bar-chapters {
      display: inline-flex;
    }
  }

  @media (max-width: 640px) {
    .rdr-anchor {
      left: 4px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .rdr-anchor {
      transition: none;
    }
  }
</style>
