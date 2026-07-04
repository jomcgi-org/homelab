<script>
  // Public chunk reader: structural render of one chunk's content (headings /
  // bullet lists / paragraphs via renderChunk), prev/next paging through the
  // whole book in reading order, and the entities mentioned on this page.
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";
  import {
    apiFetch,
    bookHref,
    chunkHref,
    entityHref,
    proxiedImageUrl,
  } from "$lib/public/grimoire/api.js";
  import { renderChunk } from "$lib/public/grimoire/renderChunk.js";

  const bookId = $derived(decodeURIComponent($page.params.book));
  const chunkId = $derived($page.params.chunk);

  let chunk = $state(null);
  let loading = $state(true);
  let error = $state("");
  // The image degrades gracefully (Constraints): if it 404s/500s (e.g. the
  // public tier's S3 creds aren't mirrored yet, see plan Task 6 risk), fall
  // back to the caption text instead of a broken-image icon.
  let imageError = $state(false);
  // Measured pager height, fed back as bottom padding so the sticky pager never
  // slices through the last lines of the reading column.
  let pagerH = $state(null);

  const blocks = $derived(chunk?.content ? renderChunk(chunk.content) : []);

  // First non-blank line of the content, used to suppress a section label that
  // merely repeats the chunk's own opening heading.
  const firstContentLine = $derived(
    (chunk?.content ?? "")
      .split("\n")
      .map((l) => l.trim())
      .find(Boolean) ?? "",
  );
  const showSection = $derived(
    !!chunk?.section_path && chunk.section_path.trim() !== firstContentLine,
  );

  $effect(() => {
    load(chunkId);
  });

  async function load(id) {
    loading = true;
    error = "";
    chunk = null;
    imageError = false;
    try {
      chunk = await apiFetch(`/chunks/${encodeURIComponent(id)}`);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  function goChunk(id) {
    if (!id) return;
    goto(chunkHref(bookId, id));
  }
</script>

<div class="reader-page page">
  {#if loading}
    <div class="wrap-narrow skeleton-wrap">
      <div class="skeleton-lines">
        {#each Array(6) as _, i (i)}
          <div class="skeleton-line"></div>
        {/each}
      </div>
    </div>
  {:else if error}
    <div class="wrap-narrow">
      <p class="mono status-error">{error}</p>
    </div>
  {:else if chunk}
    <div
      class="wrap-narrow reader-body"
      style:--pager-h={pagerH != null ? `${pagerH}px` : undefined}
    >
      <a class="eyebrow back-link" href={bookHref(bookId)}>&larr; SECTIONS</a>

      {#if chunk.image_url && !imageError}
        <figure class="figure">
          {#if showSection}
            <p class="eyebrow section-label">{chunk.section_path}</p>
          {/if}
          <img
            class="chunk-image"
            src={proxiedImageUrl(chunk.image_url)}
            alt={chunk.content || "sourcebook illustration"}
            onerror={() => (imageError = true)}
          />
          {#if chunk.content}
            <figcaption class="caption">{chunk.content}</figcaption>
          {/if}
        </figure>
      {:else}
        <div class="prose">
          {#if showSection}
            <p class="eyebrow section-label">{chunk.section_path}</p>
          {/if}
          {#if chunk.image_url && imageError}
            <p class="mono image-fallback">[ illustration unavailable ]</p>
          {/if}
          {#each blocks as block, i (i)}
            {#if block.type === "heading"}
              <h3 class="eyebrow prose-heading">{block.text}</h3>
            {:else if block.type === "list"}
              <ul class="prose-list">
                {#each block.items as item, j (j)}
                  <li>{item}</li>
                {/each}
              </ul>
            {:else}
              <p>{block.text}</p>
            {/if}
          {/each}
        </div>
      {/if}

      {#if chunk.entities?.length}
        <div class="entities-block">
          <span class="eyebrow">ON THIS PAGE</span>
          <div class="entity-chips">
            {#each chunk.entities as ent (ent.id)}
              <a class="chip mono" href={entityHref(ent.id)}>{ent.name}</a>
            {/each}
          </div>
        </div>
      {/if}
    </div>

    <nav class="pager" aria-label="Reading navigation" bind:clientHeight={pagerH}>
      <button
        class="btn pager-btn"
        disabled={!chunk.prev_id}
        onclick={() => goChunk(chunk.prev_id)}
      >
        &larr; PREV
      </button>
      <span class="pager-pos mono">
        {#if chunk.chunk_count && chunk.seq != null}
          {chunk.seq + 1} / {chunk.chunk_count}
        {:else}
          #{chunk.seq ?? "?"}
        {/if}
      </span>
      <button
        class="btn pager-btn"
        disabled={!chunk.next_id}
        onclick={() => goChunk(chunk.next_id)}
      >
        NEXT &rarr;
      </button>
    </nav>
  {/if}
</div>

<style>
  /* Structural + a handful of small primitives the design system doesn't ship
     (prose typography for arbitrary rendered content, entity chips, the
     sticky pager). Colors/fonts/borders all come from design-system tokens. */
  .reader-page {
    display: flex;
    flex-direction: column;
    min-height: 60vh;
  }

  .reader-body {
    flex: 1;
    padding-top: 32px;
    /* Clear the sticky pager: pad the bottom by its measured height so the
       last lines are never hidden under it. Falls back before measurement. */
    padding-bottom: calc(var(--pager-h, 72px) + 24px);
  }

  .back-link {
    display: inline-block;
    margin-bottom: 24px;
  }

  .back-link:hover {
    color: var(--ink);
  }

  .section-label {
    margin-bottom: 16px;
  }

  /* Body text is the workhorse sans, NOT the display serif: Instrument Serif
     is a high-contrast display face whose hairline thins all but disappear at
     body sizes, which reads as low contrast even at full ink. Hanken Grotesk
     carries long-form reading. */
  .prose {
    font-family: var(--sans);
    font-size: 18px;
    line-height: 1.7;
    color: var(--ink);
  }

  .prose p {
    margin-bottom: 18px;
  }

  .prose-heading {
    margin: 28px 0 12px;
    color: var(--ink);
  }

  .prose-heading:first-child {
    margin-top: 0;
  }

  .prose-list {
    margin: 0 0 16px 22px;
    padding: 0;
    list-style: disc;
  }

  .prose-list li {
    margin-bottom: 8px;
    padding-left: 4px;
  }

  .image-fallback {
    color: var(--ink-3);
    font-size: 13px;
    margin-bottom: 16px;
  }

  .figure {
    margin: 0;
  }

  .chunk-image {
    width: 100%;
    height: auto;
    border: 2px solid var(--ink);
  }

  .caption {
    margin-top: 12px;
    font-family: var(--sans);
    font-style: italic;
    font-size: 15px;
    color: var(--ink-2);
    text-align: center;
  }

  .entities-block {
    margin-top: 40px;
    padding-top: 20px;
    border-top: 2px solid var(--rule);
  }

  .entity-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 12px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    min-height: 36px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 6px 12px;
    border: 2px solid var(--ink);
    background: var(--blue);
    color: var(--ink);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }

  .chip:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard-sm);
  }

  .pager {
    position: sticky;
    bottom: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 16px 32px;
    background: var(--bg);
    border-top: 2px solid var(--ink);
  }

  .pager-btn {
    min-height: 44px;
    background: var(--paper);
  }

  .pager-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  .pager-btn:disabled:hover {
    transform: none;
    box-shadow: none;
  }

  .pager-pos {
    font-size: 12px;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }

  .status-error {
    color: var(--coral);
    padding: 32px 0;
  }

  .skeleton-wrap {
    padding-top: 32px;
  }

  .skeleton-lines {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .skeleton-line {
    height: 16px;
    background: linear-gradient(
      90deg,
      var(--bg-elev) 25%,
      transparent 37%,
      var(--bg-elev) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease infinite;
  }

  .skeleton-line:nth-child(3) {
    width: 80%;
  }
  .skeleton-line:nth-child(6) {
    width: 60%;
  }

  @keyframes shimmer {
    0% {
      background-position: 100% 0;
    }
    100% {
      background-position: 0 0;
    }
  }

  @media (max-width: 640px) {
    .pager {
      padding: 12px 16px;
    }
    .prose {
      font-size: 17px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-line {
      animation: none;
    }
  }
</style>
