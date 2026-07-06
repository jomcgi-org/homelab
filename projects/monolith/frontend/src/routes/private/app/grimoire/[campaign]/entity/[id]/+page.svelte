<script>
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import {
    apiFetch,
    asQuery,
    isDm,
    entitiesHref,
    entityHref,
    chunkHref,
  } from "$lib/grimoire/api.js";
  import EntityDetail from "$lib/grimoire/statblock/EntityDetail.svelte";
  import GrantChips from "$lib/grimoire/GrantChips.svelte";

  const ctx = getContext("grimoire");

  const entityId = $derived($page.params.id);

  let entity = $state(null);
  let relationships = $state([]);
  let sources = $state([]);
  let loading = $state(true);
  let error = $state("");
  let notFound = $state(false);

  $effect(() => {
    load(ctx.campaignId, entityId, ctx.viewpoint);
  });

  async function load(campaignId, id, viewpoint) {
    loading = true;
    error = "";
    notFound = false;
    entity = null;
    relationships = [];
    sources = [];
    const q = asQuery(viewpoint);
    try {
      entity = await apiFetch(`/campaigns/${campaignId}/entities/${id}?${q}`);
    } catch (e) {
      if (String(e.message).includes("not found")) notFound = true;
      else error = e.message;
      loading = false;
      return;
    }
    // Relationships + sources are best-effort; a failure there must not blank
    // the whole detail page.
    const [rels, mentions] = await Promise.allSettled([
      apiFetch(`/campaigns/${campaignId}/entities/${id}/relationships?${q}`),
      apiFetch(`/campaigns/${campaignId}/entities/${id}/mentions?${q}`),
    ]);
    relationships = rels.status === "fulfilled" ? rels.value : [];
    sources = mentions.status === "fulfilled" ? mentions.value : [];
    loading = false;
  }
</script>

<div class="detail-page">
  {#if !ctx.isDesktop}
    <a class="back" href={entitiesHref(ctx.campaignId, ctx.viewpoint)}
      >← Entities</a
    >
  {/if}

  {#if loading}
    <div class="skeleton"></div>
  {:else if notFound}
    <div class="empty">
      <p class="empty-lead">Not revealed.</p>
      <p class="empty-help">
        This entity is not part of what your character knows yet.
      </p>
    </div>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else if entity}
    <div class="columns">
      <div class="main">
        <EntityDetail {entity} />

        {#if relationships.length}
          <section class="rels">
            <h3 class="grim-smallcaps head">Relationships</h3>
            <ul class="rel-list">
              {#each relationships as rel, i (i)}
                <li class="rel" class:rel--dim={rel.entity.recognition_only}>
                  <span class="arrow">{rel.direction === "out" ? "→" : "←"}</span>
                  <span class="rel-type">{rel.rel_type.replaceAll("_", " ")}</span>
                  {#if rel.entity.recognition_only}
                    <span class="rel-name">{rel.entity.name}</span>
                    <span class="badge">name only</span>
                  {:else}
                    <a
                      class="rel-name rel-name--link"
                      href={entityHref(
                        ctx.campaignId,
                        rel.entity.id,
                        ctx.viewpoint,
                      )}>{rel.entity.name}</a
                    >
                  {/if}
                </li>
              {/each}
            </ul>
          </section>
        {/if}

        {#if sources.length}
          <section class="sources">
            <h3 class="grim-smallcaps head">Sources</h3>
            <ul class="source-list">
              {#each sources as src (src.chunk_id)}
                <li>
                  <a
                    class="source"
                    href={chunkHref(
                      ctx.campaignId,
                      src.book_id,
                      src.chunk_id,
                      ctx.viewpoint,
                    )}
                  >
                    <span class="source-where">
                      {src.section_path ?? "lore"}
                    </span>
                    <span class="source-preview">{src.preview}</span>
                  </a>
                </li>
              {/each}
            </ul>
          </section>
        {/if}
      </div>

      {#if isDm(ctx.viewpoint)}
        <aside class="aside">
          <GrantChips
            campaignId={ctx.campaignId}
            {entityId}
            {entity}
            characters={ctx.characters}
          />
        </aside>
      {/if}
    </div>
  {/if}
</div>

<style>
  .detail-page {
    padding: clamp(1rem, 4vw, 2rem);
    max-width: 68rem;
    margin: 0 auto;
  }

  .back {
    display: inline-flex;
    align-items: center;
    min-height: 2.5rem;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-accent);
    margin-bottom: 1rem;
  }

  .columns {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1.5rem;
    align-items: start;
  }

  .main {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    min-width: 0;
  }

  .head {
    font-size: 0.9rem;
    color: var(--grim-accent);
    margin-bottom: 0.6rem;
  }

  .rel-list,
  .source-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .rel {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.85rem;
  }

  .rel--dim {
    opacity: 0.5;
  }

  .arrow {
    color: var(--grim-accent);
    font-weight: 700;
  }

  .rel-type {
    font-family: var(--font-mono);
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--grim-text-faint);
  }

  .rel-name {
    font-family: var(--grim-serif);
  }

  .rel-name--link {
    color: var(--grim-accent);
    text-decoration: underline;
  }

  .badge {
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.08rem 0.35rem;
    border: 1px solid var(--grim-line);
    color: var(--grim-text-dim);
  }

  .source {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--grim-paper-line);
    background: var(--grim-surface);
    color: var(--grim-ink);
  }

  .source:hover {
    border-color: var(--grim-accent);
  }

  .source-where {
    font-family: var(--font-mono);
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--grim-text-faint);
  }

  .source-preview {
    font-family: var(--grim-serif);
    font-size: 0.85rem;
    color: var(--grim-ink-soft);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .empty {
    padding: 3rem 1rem;
    text-align: center;
  }

  .empty-lead {
    font-family: var(--grim-serif);
    font-size: 1.3rem;
    margin-bottom: 0.5rem;
  }

  .empty-help {
    font-size: 0.82rem;
    color: var(--grim-text-dim);
  }

  .status--error {
    color: var(--grim-type-creature);
    font-size: 0.8rem;
  }

  .skeleton {
    height: 18rem;
    background: linear-gradient(
      90deg,
      var(--grim-surface-2) 25%,
      transparent 37%,
      var(--grim-surface-2) 63%
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

  @media (min-width: 880px) {
    .columns {
      grid-template-columns: minmax(0, 1fr) 22rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton {
      animation: none;
    }
  }
</style>
