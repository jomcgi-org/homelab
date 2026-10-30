<script>
  // Public entity detail: fetch the full spine + typed detail, then relationships
  // and sources (mentions) best-effort (a failure there must not blank the
  // stat block). Dispatches on entity_type via EntityDetail.
  import { page } from "$app/stores";
  import { apiFetch, entityHref, chunkHref } from "$lib/public/grimoire/api.js";
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
      console.error("Could not load entity", e);
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

<div class="wrap-narrow detail-page page edition">
  {#if loading}
    <div class="skeleton-block"></div>
  {:else if notFound}
    <div class="empty">
      <p class="grim-title empty-lead">Not found.</p>
      <p class="empty-help">Nothing by that name in the books loaded here.</p>
    </div>
  {:else if error}
    <p class="status-error">
      Could not load this right now. Try again in a moment.
    </p>
  {:else if entity}
    <h1 class="sr-only">{entity.name}</h1>
    <EntityDetail {entity} />

    {#if relationships.length}
      <section class="side-section">
        <h3 class="eyebrow head">Relationships</h3>
        <ul class="rel-list">
          {#each relationships as rel, i (i)}
            <li class="rel">
              <span class="arrow">{rel.direction === "out" ? "→" : "←"}</span>
              <span class="rel-type">{rel.rel_type.replaceAll("_", " ")}</span>
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
              <a class="source" href={chunkHref(src.book_id, src.chunk_id)}>
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

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--ink-2);
    font-weight: 600;
    margin: 0;
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
    font-family: var(--font-code);
    color: var(--ink-2);
  }

  .rel-type {
    font-family: var(--font-code);
    color: var(--ink-2);
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
    text-decoration: none;
    color: inherit;
    background: var(--sheet);
    border: 1px solid var(--line);
    border-radius: 0;
    transition:
      background 0.12s,
      border-color 0.12s;
  }

  .source:hover {
    background: var(--band);
    border-color: var(--stroke);
  }

  .source-where {
    color: var(--ink-2);
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
    color: var(--ink-2);
  }

  .status-error {
    font-family: var(--font-code);
    color: var(--grim-type-creature);
    padding: 32px 0;
  }

  .skeleton-block {
    height: 320px;
    border-radius: 0;
    background: var(--band);
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
