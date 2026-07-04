<script>
  import { goto } from "$app/navigation";
  import { apiFetch, asQuery, entityHref, chunkHref } from "$lib/grimoire/api.js";

  let { campaignId, bookId, chunkId, viewpoint } = $props();

  let chunk = $state(null);
  let loading = $state(true);
  let error = $state("");

  $effect(() => {
    load(campaignId, chunkId, viewpoint);
  });

  async function load(cid, id, vp) {
    loading = true;
    error = "";
    chunk = null;
    try {
      chunk = await apiFetch(
        `/chunks/${id}?${asQuery(vp, { campaign: cid })}`,
      );
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  function goChunk(id) {
    if (!id) return;
    goto(chunkHref(campaignId, bookId, id, viewpoint));
  }
</script>

<div class="reader">
  {#if loading}
    <div class="skeleton-lines">
      {#each Array(6) as _, i (i)}
        <div class="line"></div>
      {/each}
    </div>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else if chunk}
    <div class="reader-body grim-paper">
      {#if chunk.section_path}
        <p class="section grim-smallcaps">{chunk.section_path}</p>
      {/if}

      {#if chunk.image_url}
        <figure class="figure">
          <img class="image" src={chunk.image_url} alt={chunk.content} />
          {#if chunk.content}
            <figcaption class="caption">{chunk.content}</figcaption>
          {/if}
        </figure>
      {:else}
        <div class="prose">
          {#each chunk.content.split(/\n\n+/) as para, i (i)}
            <p>{para}</p>
          {/each}
        </div>
      {/if}

      {#if chunk.entities?.length}
        <div class="entities">
          <span class="entities-label grim-smallcaps">On this page</span>
          <div class="chips">
            {#each chunk.entities as ent (ent.id)}
              <a
                class="chip"
                class:chip--dim={ent.recognition_only}
                href={entityHref(campaignId, ent.id, viewpoint)}
              >
                {ent.name}
              </a>
            {/each}
          </div>
        </div>
      {/if}
    </div>

    <nav class="pager" aria-label="Reading navigation">
      <button
        class="page-btn"
        disabled={!chunk.prev_id}
        onclick={() => goChunk(chunk.prev_id)}
      >
        ← Prev
      </button>
      <span class="seq">#{chunk.seq ?? "—"}</span>
      <button
        class="page-btn"
        disabled={!chunk.next_id}
        onclick={() => goChunk(chunk.next_id)}
      >
        Next →
      </button>
    </nav>
  {/if}
</div>

<style>
  .reader {
    display: flex;
    flex-direction: column;
    min-height: 100%;
  }

  .reader-body {
    flex: 1;
    padding: clamp(1.25rem, 5vw, 3rem) clamp(1rem, 5vw, 2rem);
  }

  .section {
    font-size: 0.72rem;
    color: var(--grim-accent);
    margin-bottom: 1rem;
  }

  .prose {
    max-width: 65ch;
    margin: 0 auto;
    font-family: var(--grim-serif);
    font-size: 1.05rem;
    line-height: 1.65;
    color: var(--grim-ink);
  }

  .prose :global(p) {
    margin-bottom: 0.85rem;
  }

  .figure {
    max-width: 46rem;
    margin: 0 auto;
  }

  .image {
    width: 100%;
    height: auto;
    border: 1px solid var(--grim-paper-line);
  }

  .caption {
    margin-top: 0.6rem;
    font-family: var(--grim-serif);
    font-style: italic;
    font-size: 0.9rem;
    color: var(--grim-ink-soft);
    text-align: center;
  }

  .entities {
    max-width: 65ch;
    margin: 2rem auto 0;
    padding-top: 1rem;
    border-top: 1px solid var(--grim-paper-line);
  }

  .entities-label {
    font-size: 0.68rem;
    color: var(--grim-ink-soft);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.5rem;
  }

  .chip {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    min-height: 2.25rem;
    display: inline-flex;
    align-items: center;
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--grim-accent);
    color: var(--grim-accent);
    background: var(--bg);
  }

  .chip--dim {
    opacity: 0.55;
    border-color: var(--grim-paper-line);
    color: var(--grim-ink-soft);
  }

  .pager {
    position: sticky;
    bottom: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: var(--bg);
    border-top: var(--border-thin);
  }

  .page-btn {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    min-height: 2.75rem;
    padding: 0.5rem 1.25rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    cursor: pointer;
  }

  .page-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  .page-btn:not(:disabled):hover {
    border-color: var(--grim-accent);
    color: var(--grim-accent);
  }

  .seq {
    font-size: 0.7rem;
    color: var(--fg-tertiary);
    font-variant-numeric: tabular-nums;
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
    padding: 1.5rem;
  }

  .skeleton-lines {
    padding: 2rem;
    max-width: 65ch;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    width: 100%;
  }

  .line {
    height: 1rem;
    background: linear-gradient(
      90deg,
      var(--surface) 25%,
      transparent 37%,
      var(--surface) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease infinite;
  }

  .line:nth-child(3) {
    width: 80%;
  }
  .line:nth-child(6) {
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

  @media (prefers-reduced-motion: reduce) {
    .line {
      animation: none;
    }
  }
</style>
