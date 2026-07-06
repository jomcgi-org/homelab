<script>
  // Section list for the private reader. Fetches the book's section
  // hierarchy once and renders a flat, indentation-by-depth list with the
  // active section highlighted. Rows link through chunkHref -> the /c/[chunk]
  // redirect, carrying the campaign and DM/player viewpoint (`?as=`) from the
  // `grimoire` context set by the campaign layout, so a click still lands in
  // the continuous reader positioned at that section's first chunk under the
  // same viewpoint the reader is in now.
  //
  // Two variants, one fetch/highlight implementation:
  //  - "sidebar" (default reading surface, desktop >760px): the list is
  //    always expanded, rendered as Reader.svelte's left section-hierarchy
  //    sidebar (see docs/plans/assets/2026-07-05-grimoire-reskin-mockup.html's
  //    Reader tab). No button, no open/close state.
  //  - "dropdown" (mobile <=760px, where the sidebar is hidden): the original
  //    compact affordance -- a "Chapters" button in the sticky bar that
  //    toggles a floating panel.
  // Styled with the clean grimoire theme (--grim-* tokens), replacing the
  // brutalist ink/cream/yellow palette this component used before this pass.
  import { getContext } from "svelte";
  import { apiFetch, chunkHref } from "$lib/grimoire/api.js";

  let { bookId, activeSectionPath = null, variant = "dropdown" } = $props();
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
  // TOC's convention: 1 = top-level "chapter" row, bold uppercase; deeper
  // rows indent).
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

<div class="rdr-chapters rdr-chapters--{variant}" bind:this={rootEl}>
  {#if variant === "dropdown"}
    <button
      type="button"
      class="rdr-chapters-btn"
      class:rdr-chapters-btn--open={open}
      aria-expanded={open}
      aria-controls="rdr-chapters-panel"
      onclick={() => (open = !open)}
    >
      Chapters
    </button>
  {/if}

  {#if variant === "sidebar" || open}
    <div class="rdr-chapters-panel" id="rdr-chapters-panel">
      {#if loading}
        <p class="rdr-chapters-status">Loading…</p>
      {:else if error}
        <p class="rdr-chapters-status">{error}</p>
      {:else if sections.length === 0}
        <p class="rdr-chapters-status">This book has no chunks yet.</p>
      {:else}
        <ul class="rdr-chapters-list">
          {#each sections as section (section.section_path)}
            {@const d = depth(section.section_path)}
            <li>
              <a
                class="rdr-chapters-row"
                class:rdr-chapters-row--h1={d <= 1}
                class:rdr-chapters-row--active={section.section_path ===
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
                <span class="rdr-chapters-row-title">{section.title}</span>
                <span class="rdr-chapters-row-count">{section.chunk_count}</span>
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

<style>
  .rdr-chapters {
    position: relative;
    flex-shrink: 0;
  }

  .rdr-chapters--sidebar {
    width: 100%;
  }

  .rdr-chapters-btn {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
    padding: 5px 14px;
    font-family: var(--font-mono);
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

  .rdr-chapters-btn:hover,
  .rdr-chapters-btn--open {
    color: var(--grim-accent);
    border-color: var(--grim-accent);
  }

  /* Self-contained dropdown, positioned relative to .rdr-chapters (this
   * component's own root). The button is the last item in the bar's
   * right-hand flex group, so right:0 lands the panel flush against the
   * bar's right edge. Width is capped against the viewport so it never
   * overflows horizontally on narrow phones. */
  .rdr-chapters-panel {
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
    font-family: var(--font-mono);
  }

  /* Sidebar variant: the panel IS the sidebar body, sticky-positioned by
   * Reader.svelte's <aside> -- reset every dropdown-only affordance so it
   * reads as a plain nav tree, not a floating card. */
  .rdr-chapters--sidebar .rdr-chapters-panel {
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

  .rdr-chapters-list {
    display: flex;
    flex-direction: column;
  }

  .rdr-chapters-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    min-height: 36px;
    padding: 7px 14px;
    border-left: 2px solid transparent;
    color: var(--grim-text-dim);
  }

  .rdr-chapters--sidebar .rdr-chapters-row {
    min-height: auto;
    padding-top: 4px;
    padding-bottom: 4px;
    padding-right: 4px;
  }

  .rdr-chapters-row:hover {
    color: var(--grim-ink);
  }

  .rdr-chapters-row--h1 {
    font-family: var(--grim-serif);
    font-weight: 600;
    color: var(--grim-ink);
  }

  .rdr-chapters-row--active {
    border-left-color: var(--grim-accent);
    color: var(--grim-accent);
    font-weight: 600;
  }

  .rdr-chapters--dropdown .rdr-chapters-row--active {
    background: var(--grim-accent-soft);
  }

  .rdr-chapters-row-title {
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .rdr-chapters-row--h1 .rdr-chapters-row-title {
    font-size: 12.5px;
  }

  .rdr-chapters-row-count {
    flex-shrink: 0;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    text-align: right;
    min-width: 22px;
    color: var(--grim-text-faint);
  }

  .rdr-chapters-status {
    padding: 14px;
    font-size: 11px;
    color: var(--grim-text-faint);
  }

  .rdr-chapters--sidebar .rdr-chapters-status {
    padding: 0;
  }
</style>
