<script>
  // Section list for the public reader. Mirrors the private tier's
  // ChaptersNav.svelte (see that file's docblock for the full rationale):
  // fetches the book's section hierarchy once and renders a flat,
  // indentation-by-depth list with the active section highlighted.
  //
  // Two variants, one fetch/highlight implementation:
  //  - "sidebar" (default reading surface, desktop >760px): the list is
  //    always expanded, rendered as Reader.svelte's left section-hierarchy
  //    sidebar (see docs/plans/assets/2026-07-05-grimoire-reskin-mockup.html's
  //    Reader tab). No button, no open/close state.
  //  - "dropdown" (mobile <=760px, where the sidebar is hidden): the original
  //    compact affordance -- a "Chapters" button in the sticky bar that
  //    toggles a floating panel.
  // Styled with the clean grimoire theme (--grim-* tokens), not the
  // site-wide brutalist design-system tokens this component used before.
  import { apiFetch, chunkHref } from "$lib/public/grimoire/api.js";

  let { bookId, activeSectionPath = null, variant = "dropdown" } = $props();

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
      {:else if sections.length === 0}
        <p class="pub-chapters-status">This book has no chunks yet.</p>
      {:else}
        <ul class="pub-chapters-list">
          {#each sections as section (section.section_path)}
            {@const d = depth(section.section_path)}
            <li>
              <a
                class="pub-chapters-row"
                class:pub-chapters-row--h1={d <= 1}
                class:pub-chapters-row--active={section.section_path ===
                  activeSectionPath}
                style:padding-left="{0.75 + (d - 1) * 0.9}rem"
                href={chunkHref(bookId, section.first_chunk_id)}
                onclick={() => (open = false)}
              >
                <span class="pub-chapters-row-title">{section.title}</span>
                <span class="pub-chapters-row-count"
                  >{section.chunk_count}</span
                >
              </a>
            </li>
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

  .pub-chapters-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    min-height: 36px;
    padding: 7px 14px;
    border-left: 2px solid transparent;
    color: var(--grim-text-dim);
  }

  .pub-chapters--sidebar .pub-chapters-row {
    min-height: auto;
    padding-top: 4px;
    padding-bottom: 4px;
    padding-right: 4px;
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

  .pub-chapters-row-count {
    flex-shrink: 0;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    text-align: right;
    min-width: 22px;
    color: var(--grim-text-faint);
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
