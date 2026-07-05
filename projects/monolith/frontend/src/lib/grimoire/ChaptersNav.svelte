<script>
  // Compact chapters dropdown, replacing the old permanent TOC pane
  // (SectionTree.svelte, since deleted): a button in the reader's sticky book
  // bar that opens a flat section list, styled exactly like the old TOC (top-
  // level rows bold mono uppercase, nested rows indented, passage counts
  // right-aligned tabular-nums). Owns its own /sections fetch (same rationale
  // as the reader's own book-meta fetch: neither host has to). Rows link
  // through the same chunkHref -> /c/[chunk] redirect the old TOC used, so a
  // click still lands in the continuous reader positioned at that section's
  // first chunk.
  import { getContext } from "svelte";
  import { apiFetch, chunkHref } from "$lib/grimoire/api.js";

  let { bookId, activeSectionPath = null } = $props();
  const ctx = getContext("grimoire");

  let open = $state(false);
  let sections = $state([]);
  let loading = $state(true);
  let error = $state("");
  let rootEl;

  $effect(() => {
    load(bookId);
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

  // Indent depth from the section_path's segment count (matches the old
  // TOC's convention: 1 = top-level "chapter" row, bold mono uppercase;
  // deeper rows indent).
  function depth(sectionPath) {
    if (!sectionPath) return 1;
    return Math.max(1, sectionPath.split("/").filter(Boolean).length);
  }

  function onWindowClick(e) {
    if (open && rootEl && !rootEl.contains(e.target)) open = false;
  }
  function onWindowKeydown(e) {
    if (e.key === "Escape") open = false;
  }
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<div class="grimb-chapters" bind:this={rootEl}>
  <button
    type="button"
    class="grimb-chapters-btn"
    class:grimb-chapters-btn--open={open}
    aria-expanded={open}
    aria-controls="grimb-chapters-panel"
    onclick={() => (open = !open)}
  >
    Chapters
  </button>

  {#if open}
    <div class="grimb-chapters-panel" id="grimb-chapters-panel">
      {#if loading}
        <p class="grimb-chapters-status">Loading…</p>
      {:else if error}
        <p class="grimb-chapters-status">{error}</p>
      {:else if sections.length === 0}
        <p class="grimb-chapters-status">This book has no chunks yet.</p>
      {:else}
        <ul class="grimb-chapters-list">
          {#each sections as section (section.section_path)}
            {@const d = depth(section.section_path)}
            <li>
              <a
                class="grimb-chapters-row"
                class:grimb-chapters-row--h1={d <= 1}
                class:grimb-chapters-row--active={section.section_path ===
                  activeSectionPath}
                style:padding-left="{0.75 + (d - 1) * 0.9}rem"
                href={chunkHref(
                  ctx.campaignId,
                  bookId,
                  section.first_chunk_id,
                  ctx.viewpoint,
                )}
                onclick={() => (open = false)}
              >
                <span class="grimb-chapters-row-title">{section.title}</span>
                <span class="grimb-chapters-row-count">{section.chunk_count}</span>
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

<style>
  .grimb-chapters {
    position: relative;
    flex-shrink: 0;
  }

  .grimb-chapters-btn {
    display: inline-flex;
    align-items: center;
    min-height: 2rem;
    padding: 0.3rem 0.7rem;
    font-family: var(--grimb-mono);
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    background: var(--grimb-cream);
    color: var(--grimb-ink);
    border: var(--grimb-border);
    cursor: pointer;
  }

  .grimb-chapters-btn--open {
    background: var(--grimb-yellow);
  }

  /* Self-contained dropdown: positioned relative to .grimb-chapters (this
   * component's own root), not the bar, so it never depends on the host
   * bar's own positioning. The button is the last item in the bar's
   * right-hand flex group, so right:0 already lands the panel flush against
   * the bar's right edge. Width is capped against the viewport so it never
   * overflows horizontally on narrow phones. */
  .grimb-chapters-panel {
    position: absolute;
    top: 100%;
    right: 0;
    z-index: 6;
    margin-top: 0.4rem;
    width: min(20rem, calc(100vw - 2.5rem));
    max-height: 60vh;
    overflow-y: auto;
    background: var(--grimb-paper);
    border: var(--grimb-border);
    font-family: var(--grimb-mono);
  }

  .grimb-chapters-list {
    display: flex;
    flex-direction: column;
  }

  .grimb-chapters-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
    min-height: 2.5rem;
    padding: 0.5rem 0.85rem;
    border-left: 4px solid transparent;
    color: var(--grimb-ink-3);
  }

  .grimb-chapters-row:hover {
    color: var(--grimb-ink);
  }

  .grimb-chapters-row--h1 {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--grimb-ink);
  }

  .grimb-chapters-row--active {
    background: var(--grimb-yellow);
    border-left-color: var(--grimb-ink);
    color: var(--grimb-ink);
  }

  .grimb-chapters-row-title {
    font-size: 0.85rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .grimb-chapters-row--h1 .grimb-chapters-row-title {
    font-size: 0.8rem;
  }

  .grimb-chapters-row-count {
    flex-shrink: 0;
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    text-align: right;
    min-width: 1.5rem;
  }

  .grimb-chapters-status {
    padding: 1rem 0.85rem;
    font-size: 0.72rem;
    color: var(--grimb-ink-3);
  }
</style>
