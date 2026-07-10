<script>
  // Section list for the public reader. Mirrors the private tier's
  // ChaptersNav.svelte (see that file's docblock for the full rationale):
  // fetches the book's section hierarchy once, then folds the flat
  // {section_path, title, ...} rows into a two-level chapter/leaf tree via
  // section-tree.js's buildSectionTree (pure, unit-tested separately).
  //
  // Two variants, one fetch/highlight/tree implementation:
  //  - "sidebar" (default reading surface, desktop >900px): the tree is
  //    rendered as Reader.svelte's left TOC column. Chapter rows are
  //    collapsible buttons (chevron, aria-expanded); every chapter starts
  //    collapsed except the one containing the current section, which
  //    auto-expands. Leaf rows carry no chunk-count number (removed per the
  //    showcase redesign: the count was noise, not a reading aid).
  //  - "dropdown" (mobile <=900px, where the sidebar is hidden): the same
  //    tree, same collapse behavior, in a floating panel behind a "Chapters"
  //    toggle button in the sticky bar.
  // Styled with the clean grimoire theme (--grim-* tokens).
  import { apiFetch, chunkHref } from "$lib/public/grimoire/api.js";
  import { buildSectionTree } from "$lib/public/grimoire/book/section-tree.js";

  let { bookId, activeSectionPath = null, variant = "dropdown" } = $props();

  let open = $state(false);
  let sections = $state([]);
  let loading = $state(true);
  let error = $state("");
  // Chapter titles currently expanded. Recomputed (not just initialized)
  // whenever the active section moves into a different chapter, so scrolling
  // into a new chapter auto-opens it without clobbering a chapter the visitor
  // opened by hand elsewhere in the tree.
  let expanded = $state(new Set());
  let rootEl;

  const tree = $derived(buildSectionTree(sections));

  $effect(() => {
    load(bookId);
  });

  // Auto-expand the chapter containing the active section. Runs whenever
  // activeSectionPath or the tree changes; adds (never removes) so a manual
  // toggle elsewhere in the tree survives scroll-driven updates.
  $effect(() => {
    const path = activeSectionPath;
    if (!path) return;
    const chapterTitle = path.includes("/") ? path.split("/")[0] : null;
    if (!chapterTitle) return;
    if (!expanded.has(chapterTitle)) {
      expanded = new Set(expanded).add(chapterTitle);
    }
  });

  async function load(id) {
    loading = true;
    error = "";
    try {
      sections = await apiFetch(`/books/${encodeURIComponent(id)}/sections`);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  function toggleChapter(title) {
    const next = new Set(expanded);
    if (next.has(title)) next.delete(title);
    else next.add(title);
    expanded = next;
  }

  function onWindowClick(e) {
    if (open && rootEl && !rootEl.contains(e.target)) open = false;
  }
  function onWindowKeydown(e) {
    if (e.key === "Escape") open = false;
  }
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<div class="pub-chapters pub-chapters--{variant}" bind:this={rootEl}>
  {#if variant === "dropdown"}
    <button
      type="button"
      class="pub-chapters-btn"
      class:pub-chapters-btn--open={open}
      aria-expanded={open}
      aria-controls="pub-chapters-panel"
      onclick={() => (open = !open)}
    >
      Chapters
    </button>
  {/if}

  {#if variant === "sidebar" || open}
    <div class="pub-chapters-panel" id="pub-chapters-panel">
      {#if loading}
        <p class="pub-chapters-status">Loading…</p>
      {:else if error}
        <p class="pub-chapters-status">{error}</p>
      {:else if tree.length === 0}
        <p class="pub-chapters-status">This book has no chunks yet.</p>
      {:else}
        <ul class="pub-chapters-list">
          {#each tree as node (node.section ? node.section.section_path : node.title)}
            {#if node.children.length === 0}
              <li>
                <a
                  class="pub-chapters-row pub-chapters-row--h1"
                  class:pub-chapters-row--active={node.section?.section_path ===
                    activeSectionPath}
                  href={chunkHref(bookId, node.section?.first_chunk_id)}
                  onclick={() => (open = false)}
                >
                  <span class="pub-chapters-row-title">{node.title}</span>
                </a>
              </li>
            {:else}
              {@const isOpen = expanded.has(node.title)}
              <li>
                <button
                  type="button"
                  class="pub-chapters-chapter"
                  aria-expanded={isOpen}
                  aria-controls={"pub-chapter-" + node.title}
                  onclick={() => toggleChapter(node.title)}
                >
                  <svg
                    class="pub-chapters-chevron"
                    class:pub-chapters-chevron--open={isOpen}
                    width="10"
                    height="10"
                    viewBox="0 0 10 10"
                    aria-hidden="true"
                  >
                    <path
                      d="M2 1 L7 5 L2 9"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.6"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                  <span class="pub-chapters-row-title">{node.title}</span>
                </button>
                {#if isOpen}
                  <ul
                    class="pub-chapters-list pub-chapters-list--nested"
                    id={"pub-chapter-" + node.title}
                  >
                    {#each node.children as child (child.section.section_path)}
                      <li>
                        <a
                          class="pub-chapters-row"
                          class:pub-chapters-row--active={child.section
                            .section_path === activeSectionPath}
                          href={chunkHref(bookId, child.section.first_chunk_id)}
                          onclick={() => (open = false)}
                        >
                          <span class="pub-chapters-row-title"
                            >{child.title}</span
                          >
                        </a>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </li>
            {/if}
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

<style>
  .pub-chapters {
    position: relative;
    flex-shrink: 0;
  }

  .pub-chapters--sidebar {
    width: 100%;
  }

  .pub-chapters-btn {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
    padding: 5px 14px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    background: var(--grim-surface);
    color: var(--grim-ink);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    cursor: pointer;
  }

  .pub-chapters-btn:hover,
  .pub-chapters-btn--open {
    color: var(--grim-accent);
    border-color: var(--grim-accent);
  }

  /* Self-contained dropdown, positioned relative to .pub-chapters (this
   * component's own root). The button is the last item in the bar's
   * right-hand flex group, so right:0 lands the panel flush against the
   * bar's right edge. Width is capped against the viewport so it never
   * overflows horizontally on narrow phones. */
  .pub-chapters-panel {
    position: absolute;
    top: 100%;
    right: 0;
    z-index: 6;
    margin-top: 6px;
    width: min(320px, calc(100vw - 40px));
    max-height: 60vh;
    overflow-y: auto;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(20, 24, 32, 0.14);
    /* Base reading face for nested rows; top-level rows opt into the serif
     * display face below. */
    font-family: var(--sans);
  }

  /* Sidebar variant: the panel IS the sidebar body, sticky-positioned by
   * Reader.svelte's <aside> -- reset every dropdown-only affordance so it
   * reads as a plain nav tree, not a floating card. */
  .pub-chapters--sidebar .pub-chapters-panel {
    position: static;
    width: 100%;
    max-height: none;
    overflow: visible;
    background: transparent;
    border: 0;
    border-radius: 0;
    box-shadow: none;
    margin-top: 0;
  }

  .pub-chapters-list {
    display: flex;
    flex-direction: column;
  }

  .pub-chapters-list--nested {
    margin-left: 0.25rem;
  }

  .pub-chapters-row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    min-height: 36px;
    padding: 7px 14px;
    border-left: 2px solid transparent;
    color: var(--grim-text-dim);
  }

  .pub-chapters--sidebar .pub-chapters-row {
    min-height: auto;
    padding: 4px 4px 4px 1.65rem;
  }

  .pub-chapters--sidebar .pub-chapters-row--h1 {
    padding-left: 0.75rem;
  }

  .pub-chapters-row:hover {
    color: var(--grim-ink);
  }

  .pub-chapters-row--h1 {
    font-family: var(--grim-serif);
    font-weight: 600;
    color: var(--grim-ink);
  }

  .pub-chapters-row--active {
    border-left-color: var(--grim-accent);
    color: var(--grim-accent);
    font-weight: 600;
  }

  .pub-chapters--dropdown .pub-chapters-row--active {
    background: var(--grim-accent-soft);
  }

  .pub-chapters-row-title {
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pub-chapters-row--h1 .pub-chapters-row-title {
    font-size: 12.5px;
  }

  /* Chapter toggle: same visual rhythm as a leaf row (button, not a div, so
   * it's keyboard reachable with a plain Tab + Enter/Space), plus a chevron
   * that rotates open/closed. */
  .pub-chapters-chapter {
    display: flex;
    align-items: baseline;
    gap: 8px;
    width: 100%;
    min-height: 36px;
    padding: 7px 14px;
    background: none;
    border: 0;
    border-left: 2px solid transparent;
    font: inherit;
    text-align: left;
    color: var(--grim-ink);
    cursor: pointer;
  }

  .pub-chapters--sidebar .pub-chapters-chapter {
    min-height: auto;
    padding: 4px 4px 4px 0.75rem;
  }

  .pub-chapters-chapter:hover {
    color: var(--grim-accent);
  }

  .pub-chapters-chapter .pub-chapters-row-title {
    font-family: var(--grim-serif);
    font-weight: 600;
    font-size: 12.5px;
  }

  .pub-chapters-chevron {
    flex-shrink: 0;
    align-self: center;
    color: var(--grim-text-faint);
    transform: rotate(0deg);
    transition: transform 140ms ease;
  }

  .pub-chapters-chevron--open {
    transform: rotate(90deg);
  }

  @media (prefers-reduced-motion: reduce) {
    .pub-chapters-chevron {
      transition: none;
    }
  }

  .pub-chapters-status {
    padding: 14px;
    font-size: 11px;
    color: var(--grim-text-faint);
  }

  .pub-chapters--sidebar .pub-chapters-status {
    padding: 0;
  }
</style>
