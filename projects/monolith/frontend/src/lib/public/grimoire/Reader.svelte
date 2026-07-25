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
  // Layout (showcase redesign): a left TOC sidebar (desktop, >900px)
  // alongside a wider ~72ch reading column, laid out as one centered grid
  // using the available width. No sub-nav strip: the old bar with the book
  // name and a "SECTION n/total" position readout is gone. A 2px
  // scroll-fraction progress bar (fixed under the app topbar) replaces it as
  // the sole reading-position indicator -- cheaper to compute (one
  // scroll-fraction number, transform: scaleX, no reflow) and less noisy
  // than a text readout that updated on every heading crossing. Below 900px
  // the sidebar hides and the dropdown Chapters affordance reappears (in a
  // slim floating toggle, since the sticky bar itself is gone).
  import { apiFetch, bookAttribution } from "$lib/public/grimoire/api.js";
  import { renderChunk } from "$lib/public/grimoire/renderChunk.js";
  import { highlightMentions } from "$lib/public/grimoire/chat/mention-highlight.js";
  import ChaptersNav from "$lib/public/grimoire/ChaptersNav.svelte";
  import { buildSectionTree } from "$lib/public/grimoire/book/section-tree.js";

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

  // Section tree for both ChaptersNav mounts below (mobile dropdown + desktop
  // sidebar): fetched and built ONCE here rather than once per mount, since
  // the two instances would otherwise issue duplicate GETs and duplicate
  // tree builds for identical data.
  let sectionsLoading = $state(true);
  let sectionsError = $state("");
  let sections = $state([]);
  const sectionTree = $derived(buildSectionTree(sections));

  $effect(() => {
    loadSections(bookId);
  });

  async function loadSections(id) {
    sectionsLoading = true;
    sectionsError = "";
    try {
      sections = await apiFetch(`/books/${encodeURIComponent(id)}/sections`);
    } catch (e) {
      sectionsError = e.message;
    } finally {
      sectionsLoading = false;
    }
  }

  // Reading-progress bar: scroll fraction of the content column, 0..1.
  // scaleX (not width) on a fixed-width track so the browser only ever
  // composites a transform, never reflows layout on scroll.
  let progress = $state(0);
  let progressVisible = $state(false);

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
  // alone). The reader already shows that name as the section <h2>, so a
  // leading block that merely repeats it renders the title twice. Drop it,
  // comparing case-insensitively against both the full section_path and its
  // last "/" segment (the visible title).
  function bodyBlocks(item) {
    const blocks = renderChunk(item.content);
    const first = blocks[0];
    if (!first || (first.type !== "heading" && first.type !== "para"))
      return blocks;
    const norm = (s) => (s ?? "").trim().toUpperCase();
    const head = norm(first.text);
    if (
      head &&
      (head === norm(item.section_path) ||
        head === norm(sectionTitle(item.section_path)))
    ) {
      return blocks.slice(1);
    }
    return blocks;
  }

  // Chunk content is book text straight from the DB: it must be HTML-escaped
  // before any {@html} render. Mirrors mention-highlight.js's own escapeHtml
  // (that module isn't exported for reuse, its contract is "call on fresh
  // renderMarkdown output only", so this is a deliberate small duplicate
  // rather than reaching into its internals).
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Backend entities are {id, name, entity_type, mention_text}; highlightMentions
  // expects {id, title, kind: "entity", entity_type}. Also dedupes by name: a
  // chunk can carry more than one mention row for the same entity (e.g. two
  // mention_text spellings), and highlightMentions only needs one per name.
  function touchedEntities(entities) {
    if (!entities?.length) return [];
    const seen = new Set();
    const touched = [];
    for (const e of entities) {
      if (seen.has(e.name)) continue;
      seen.add(e.name);
      touched.push({
        id: e.id,
        title: e.name,
        kind: "entity",
        entity_type: e.entity_type,
      });
    }
    return touched;
  }

  // Mention-linkified HTML for one block of plain text, escaping first so
  // book text can never inject markup (the {@html} XSS boundary), then
  // wrapping any touched entity names in the same type-colored links the
  // chat surface uses.
  function markText(text, touched) {
    return highlightMentions(escapeHtml(text ?? ""), touched);
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
            (a, b) =>
              Number(b.target.dataset.seq) - Number(a.target.dataset.seq),
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
    el.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
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
    el.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
  });

  // Reading-progress bar. The app root scrolls the window (see the
  // IntersectionObserver comment above), so progress is the content
  // column's own scrolled fraction: how far containerEl's top has traveled
  // above the viewport versus its total scrollable height. rAF-throttled so
  // a scroll storm never queues more than one recompute per frame; the write
  // itself is a single scaleX transform (see .pub-progress-fill), so there
  // is no layout thrash to throttle beyond that. Hidden entirely when the
  // content already fits the viewport (nothing to scroll).
  $effect(() => {
    void items.length;
    if (!containerEl || typeof window === "undefined") return;
    let ticking = false;
    function measure() {
      ticking = false;
      const rect = containerEl.getBoundingClientRect();
      const total = rect.height - window.innerHeight;
      if (total <= 0) {
        progressVisible = false;
        progress = 0;
        return;
      }
      progressVisible = true;
      const scrolled = -rect.top;
      progress = Math.min(1, Math.max(0, scrolled / total));
    }
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(measure);
    }
    measure();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  });
</script>

<div class="pub-reader">
  <!-- Reading-progress bar: fixed under the app topbar (58px, see
       +layout.svelte's `.topbar`). scaleX only -- never width -- so a scroll
       storm is pure compositor work, no reflow. Hidden entirely when the
       content already fits the viewport. -->
  <div
    class="pub-progress"
    class:pub-progress--visible={progressVisible}
    role="progressbar"
    aria-label="Reading progress"
    aria-valuenow={Math.round(progress * 100)}
    aria-valuemin="0"
    aria-valuemax="100"
  >
    <div class="pub-progress-fill" style:transform="scaleX({progress})"></div>
  </div>

  <!-- Compact mobile-only affordance: the sidebar below is hidden under
       900px, so this floating Chapters button is the only way to jump
       sections on a phone (the old sticky bar that used to host it is
       gone). Hidden on desktop via CSS (the sidebar replaces it there);
       still mounts regardless of viewport, but shares the one sections
       fetch/tree above with the sidebar instance rather than each mount
       fetching and building independently. -->
  <div class="pub-mobile-chapters">
    <ChaptersNav
      {bookId}
      tree={sectionTree}
      loading={sectionsLoading}
      error={sectionsError}
      {activeSectionPath}
      variant="dropdown"
    />
  </div>

  <div class="pub-layout">
    <aside class="pub-toc" aria-label="Sections">
      <p class="pub-toc-label">{bookMeta.displayName}</p>
      <ChaptersNav
        {bookId}
        tree={sectionTree}
        loading={sectionsLoading}
        error={sectionsError}
        {activeSectionPath}
        variant="sidebar"
      />
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
            <h2 class="pub-section-title">
              {sectionTitle(row.item.section_path)}
            </h2>
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
                  loading="lazy"
                />
              {/if}
              {#if row.item.content}
                <figcaption class="pub-caption">{row.item.content}</figcaption>
              {/if}
            </figure>
          {:else}
            {@const touched = touchedEntities(row.item.entities)}
            {#each bodyBlocks(row.item) as block, bi (bi)}
              {#if block.type === "heading"}
                {#if touched.length}
                  <h3 class="pub-inline-heading">
                    {@html markText(block.text, touched)}
                  </h3>
                {:else}
                  <h3 class="pub-inline-heading">{block.text}</h3>
                {/if}
              {:else if block.type === "list"}
                <ul class="pub-list">
                  {#each block.items as li, lii (lii)}
                    {#if touched.length}
                      <li>{@html markText(li, touched)}</li>
                    {:else}
                      <li>{li}</li>
                    {/if}
                  {/each}
                </ul>
              {:else if touched.length}
                <p>{@html markText(block.text, touched)}</p>
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
          {#if bookAttribution(bookId)}
            <p class="pub-attribution">{bookAttribution(bookId)}</p>
          {/if}
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

  /* Reading-progress bar: fixed directly under the app topbar (58px, see
     +layout.svelte's `.topbar`). A track + a scaleX-transformed fill,
     transform-origin left, so the only per-scroll write is a compositor-only
     transform -- no width/layout property ever changes. */
  .pub-progress {
    position: fixed;
    top: 58px;
    left: 0;
    right: 0;
    z-index: 6;
    height: 2px;
    background: var(--grim-line-soft);
    opacity: 0;
    transition: opacity 160ms ease;
    pointer-events: none;
  }

  .pub-progress--visible {
    opacity: 1;
  }

  .pub-progress-fill {
    height: 100%;
    width: 100%;
    background: var(--grim-accent);
    transform: scaleX(0);
    transform-origin: left center;
  }

  @media (prefers-reduced-motion: reduce) {
    .pub-progress {
      transition: none;
    }
  }

  /* Floating mobile-only Chapters affordance. The sidebar below is hidden
     under 900px, so this is the only way to jump sections on a phone (the
     old sticky sub-nav bar that used to host it is gone). */
  .pub-mobile-chapters {
    display: none;
  }

  .pub-layout {
    max-width: 1180px;
    margin: 0 auto;
    padding: clamp(24px, 5vw, 48px) clamp(20px, 6vw, 48px) 80px;
    display: grid;
    grid-template-columns: 280px minmax(0, 72ch);
    justify-content: center;
    gap: 56px;
    align-items: start;
  }

  .pub-toc {
    position: sticky;
    /* 58px app topbar + 2px progress bar + a little breathing room. */
    top: 76px;
    max-height: calc(100vh - 116px);
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
    max-width: 72ch;
    margin: 28px 0;
    height: 1px;
    border: 0;
    background: var(--grim-line);
  }

  .pub-heading {
    position: relative;
    max-width: 72ch;
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
    max-width: 72ch;
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

  /* Entity mentions (highlightMentions): matches the chat surface's .gmark
     treatment (routes/public/app/grimoire/chat/+page.svelte) so a mention
     reads the same whether it's in a reply or the book text. The color rides
     in via the inline `color` style the module sets per anchor; the pill
     background and underline both derive from it (currentColor) via
     color-mix, so a new entity type never needs a new rule here. */
  .pub-chunk :global(.gmark) {
    font-weight: 600;
    text-decoration: underline;
    text-decoration-thickness: 1.5px;
    text-underline-offset: 2px;
    border-radius: 4px;
    padding: 0.5px 4px;
    margin: 0 -1px;
    background: color-mix(in srgb, currentColor 12%, transparent);
    transition: background 120ms ease;
  }

  .pub-chunk :global(.gmark:hover),
  .pub-chunk :global(.gmark:focus-visible) {
    background: color-mix(in srgb, currentColor 20%, transparent);
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
    max-width: 72ch;
  }

  /* Full column width: the wider ~72ch column gives illustrations real
     presence, so images fill it (width: 100%, not an auto-sized max-width)
     rather than sitting small and centered. max-height still caps oversized
     scans so a tall scan never dominates the whole viewport. */
  .pub-image {
    display: block;
    width: 100%;
    height: auto;
    max-height: 70vh;
    object-fit: contain;
    border: 1px solid var(--grim-line);
    border-radius: 10px;
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
    max-width: 72ch;
  }

  .pub-status {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--grim-text-faint);
  }

  /* License attribution required by CC BY 4.0 / ORC for the open books that
     are readable in full. Quiet, but always present at the end of the text. */
  .pub-attribution {
    max-width: 640px;
    margin: 14px auto 0;
    font-size: 11.5px;
    line-height: 1.5;
    text-align: center;
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

  @media (max-width: 900px) {
    .pub-layout {
      grid-template-columns: minmax(0, 72ch);
      padding-top: 20px;
    }

    .pub-toc {
      display: none;
    }

    .pub-mobile-chapters {
      display: flex;
      justify-content: flex-end;
      padding: 10px clamp(20px, 6vw, 48px) 0;
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
