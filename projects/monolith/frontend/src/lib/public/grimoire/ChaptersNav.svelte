<script>
  // Compact chapters dropdown for the public reader. Mirrors the private
  // tier's ChaptersNav.svelte (see that file's docblock for the full
  // rationale): a button in the sticky book bar opens a flat section list,
  // styled like the old public section-list page (mono uppercase top-level
  // rows, indented nested rows, right-aligned chunk counts). Reuses the
  // public design-system tokens directly, same as Reader.svelte, so no new
  // CSS variables are needed here.
  import { apiFetch, chunkHref } from "$lib/public/grimoire/api.js";

  let { bookId, activeSectionPath = null } = $props();

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

<div class="pub-chapters" bind:this={rootEl}>
  <button
    type="button"
    class="mono pub-chapters-btn"
    class:pub-chapters-btn--open={open}
    aria-expanded={open}
    aria-controls="pub-chapters-panel"
    onclick={() => (open = !open)}
  >
    Chapters
  </button>

  {#if open}
    <div class="pub-chapters-panel" id="pub-chapters-panel">
      {#if loading}
        <p class="mono pub-chapters-status">Loading…</p>
      {:else if error}
        <p class="mono pub-chapters-status">{error}</p>
      {:else if sections.length === 0}
        <p class="mono pub-chapters-status">This book has no chunks yet.</p>
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
                <span class="mono pub-chapters-row-count"
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

  .pub-chapters-btn {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
    padding: 5px 11px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    background: var(--cream);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
  }

  .pub-chapters-btn--open {
    background: var(--accent);
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
    background: var(--paper);
    border: 2px solid var(--ink);
    /* Reset from the ancestor .pub-bar's `mono` class: nested rows read in
     * the sans reading face, top-level rows opt back into mono below. */
    font-family: var(--sans);
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
    min-height: 40px;
    padding: 8px 14px;
    border-left: 4px solid transparent;
    color: var(--ink-3);
  }

  .pub-chapters-row:hover {
    color: var(--ink);
  }

  .pub-chapters-row--h1 {
    font-family: var(--mono);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--ink);
  }

  .pub-chapters-row--active {
    background: var(--accent);
    border-left-color: var(--ink);
    color: var(--ink);
  }

  .pub-chapters-row-title {
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pub-chapters-row--h1 .pub-chapters-row-title {
    font-size: 12px;
  }

  .pub-chapters-row-count {
    flex-shrink: 0;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    text-align: right;
    min-width: 22px;
  }

  .pub-chapters-status {
    padding: 16px 14px;
    font-size: 11px;
    color: var(--ink-3);
  }
</style>
