<script>
  // Public entity detail: fetch the full spine + typed detail, then relationships
  // and sources (mentions) best-effort (a failure there must not blank the
  // stat block). Dispatches on entity_type via EntityDetail.
  import { page } from "$app/stores";
  import {
    apiFetch,
    entitiesHref,
    entityHref,
    chunkHref,
  } from "$lib/public/grimoire/api.js";
  import EntityDetail from "$lib/public/grimoire/statblock/EntityDetail.svelte";

  const entityId = $derived($page.params.id);

  let entity = $state(null);
  let relationships = $state([]);
  let sources = $state([]);
  let loading = $state(true);
  let error = $state("");
  let notFound = $state(false);

  $effect(() => {
    load(entityId);
  });

  async function load(id) {
    loading = true;
    error = "";
    notFound = false;
    entity = null;
    relationships = [];
    sources = [];
    try {
      entity = await apiFetch(`/entities/${encodeURIComponent(id)}`);
    } catch (e) {
      const msg = String(e.message);
      if (msg.includes("404") || msg.toLowerCase().includes("not found")) {
        notFound = true;
      } else {
        error = e.message;
      }
      loading = false;
      return;
    }
    // Relationships + sources are best-effort; a failure there must not blank
    // the whole detail page.
    const [rels, mentions] = await Promise.allSettled([
      apiFetch(`/entities/${encodeURIComponent(id)}/relationships`),
      apiFetch(`/entities/${encodeURIComponent(id)}/mentions`),
    ]);
    relationships = rels.status === "fulfilled" ? (rels.value ?? []) : [];
    sources = mentions.status === "fulfilled" ? (mentions.value ?? []) : [];
    loading = false;
  }
</script>

<div class="wrap-narrow detail-page page">
  <a class="eyebrow back-link" href={entitiesHref()}>&larr; ENTITIES</a>

  {#if loading}
    <div class="card-hard skeleton-block"></div>
  {:else if notFound}
    <div class="empty">
      <p class="empty-lead display">Not found.</p>
      <p class="empty-help">This entity isn't in the loaded corpus.</p>
    </div>
  {:else if error}
    <p class="mono status-error">{error}</p>
  {:else if entity}
    <EntityDetail {entity} />

    {#if relationships.length}
      <section class="side-section">
        <h3 class="eyebrow head">Relationships</h3>
        <ul class="rel-list">
          {#each relationships as rel, i (i)}
            <li class="rel">
              <span class="arrow mono">{rel.direction === "out" ? "→" : "←"}</span>
              <span class="mono rel-type">{rel.rel_type.replaceAll("_", " ")}</span>
              <a class="rel-name" href={entityHref(rel.entity.id)}
                >{rel.entity.name}</a
              >
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    {#if sources.length}
      <section class="side-section">
        <h3 class="eyebrow head">Sources</h3>
        <ul class="source-list">
          {#each sources as src, i (i)}
            <li>
              <a
                class="card-hard source"
                href={chunkHref(src.book_id, src.chunk_id)}
              >
                <span class="eyebrow source-where">
                  {src.section_path ?? "lore"}
                </span>
                <span class="source-preview">{src.preview}</span>
              </a>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</div>

<style>
  .detail-page {
    padding: 48px 32px 96px;
    display: flex;
    flex-direction: column;
    gap: 32px;
  }

  .back-link {
    display: inline-block;
    align-self: flex-start;
  }

  .back-link:hover {
    color: var(--ink);
  }

  .head {
    margin-bottom: 12px;
  }

  .rel-list,
  .source-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .rel {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 32px;
  }

  .arrow {
    color: var(--ink-3);
  }

  .rel-type {
    color: var(--ink-3);
    font-size: 12px;
  }

  .rel-name {
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  .rel-name:hover {
    color: var(--ink);
  }

  .source {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 14px 16px;
    min-height: 44px;
  }

  .source-where {
    color: var(--ink-3);
  }

  .source-preview {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    color: var(--ink-2);
  }

  .empty {
    padding: 64px 0;
    text-align: center;
  }

  .empty-lead {
    font-size: 28px;
    margin-bottom: 8px;
  }

  .empty-help {
    color: var(--ink-3);
  }

  .status-error {
    color: var(--coral);
    padding: 32px 0;
  }

  .skeleton-block {
    height: 320px;
    background: linear-gradient(
      90deg,
      var(--bg-elev) 25%,
      transparent 37%,
      var(--bg-elev) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease infinite;
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
    .skeleton-block {
      animation: none;
    }
  }
</style>
