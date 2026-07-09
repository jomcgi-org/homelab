<script>
  // In-place preview codex for the entities index: opens under a clicked card
  // (see +page.svelte) instead of navigating away. Relationship pills
  // re-target `currentId` IN PLACE so the row never closes/reopens or moves
  // on screen while browsing a cluster of entities.
  //
  // This is a PREVIEW, not the full statblock: a short clamped summary plus a
  // pre-settled ego graph. "Full entry" links to the real detail page for the
  // complete EntityDetail render.
  import {
    apiFetch,
    exploreEgo,
    entityHref,
    exploreHref,
  } from "$lib/public/grimoire/api.js";
  import MiniConstellation from "$lib/public/grimoire/MiniConstellation.svelte";

  let { entityId, onnavigatehint = null } = $props();

  let currentId = $state(entityId);
  $effect(() => {
    currentId = entityId;
  });

  let entity = $state(null);
  let relationships = $state([]);
  let egoNodes = $state([]);
  let egoEdges = $state([]);
  let loading = $state(false);
  let error = $state("");

  $effect(() => {
    const id = currentId;
    if (!id) return;
    load(id);
  });

  async function load(id) {
    loading = true;
    error = "";
    entity = null;
    relationships = [];
    egoNodes = [];
    egoEdges = [];
    // Detail and ego are independent, best-effort fetches (mirror
    // ExploreCodex.svelte:54-61): a failed ego must not blank a loaded
    // statblock, and a failed detail must not block the relationship graph.
    const [entityRes, egoRes] = await Promise.allSettled([
      apiFetch(`/entities/${encodeURIComponent(id)}`),
      exploreEgo(id),
    ]);
    if (entityRes.status === "fulfilled") {
      entity = entityRes.value;
    } else {
      error = entityRes.reason?.message ?? "Failed to load entity.";
    }
    if (egoRes.status === "fulfilled") {
      egoNodes = egoRes.value?.nodes ?? [];
      egoEdges = egoRes.value?.edges ?? [];
      relationships = buildRelationships(id, egoRes.value);
    }
    loading = false;
  }

  // Same 15-line resolve as ExploreCodex.svelte's buildRelationships: ego
  // edges are undirected {from, to, rel_type} pairs, resolved into "the
  // other entity" plus arrow direction relative to the focus.
  function buildRelationships(id, ego) {
    const nodesById = new Map((ego?.nodes ?? []).map((n) => [n.id, n]));
    return (ego?.edges ?? [])
      .map((e) => {
        const out = e.from === id;
        const peer = nodesById.get(out ? e.to : e.from);
        if (!peer) return null;
        return { rel_type: e.rel_type, peer, out };
      })
      .filter(Boolean)
      .sort(
        (a, b) =>
          a.rel_type.localeCompare(b.rel_type) ||
          a.peer.name.localeCompare(b.peer.name),
      );
  }

  function retarget(peerId) {
    currentId = peerId;
    onnavigatehint?.(peerId);
  }

  function typeLabel(entityType) {
    return entityType.replaceAll("_", " ");
  }

  function swatch(entityType) {
    return `background: var(--grim-type-${entityType}, var(--grim-text-faint))`;
  }

  // The public entity payload is flat JSONB with no dedicated "summary"
  // field; `description` is the field format.js's isProse() treats as long
  // prose (see Generic.svelte/Spell.svelte). Only render it here when it is
  // actually a string -- some entity types carry `description` as a
  // blocks-shaped array/object instead, and this preview never renders
  // blocks (that is the "Full entry" statblock's job).
  const summary = $derived(
    typeof entity?.description === "string" ? entity.description : "",
  );
</script>

<div class="codex" aria-live="polite">
  {#if loading}
    <div class="skeleton" aria-hidden="true"></div>
  {:else if error}
    <p class="status-error">{error}</p>
  {:else if entity}
    <div class="cols">
      <div class="left">
        <div class="type-row">
          <span class="sw" style={swatch(entity.entity_type)}></span>
          <span class="eyebrow">{typeLabel(entity.entity_type)}</span>
        </div>
        <h3 class="name">{entity.name}</h3>
        {#if summary}
          <p class="summary">{summary}</p>
        {/if}

        {#if relationships.length}
          <div class="pills">
            {#each relationships as r, i (r.peer.id + "|" + r.rel_type + "|" + r.out + "|" + i)}
              <button
                type="button"
                class="pill"
                onclick={() => retarget(r.peer.id)}
              >
                <span class="sw" style={swatch(r.peer.entity_type)}></span>
                <span class="peer-name">{r.peer.name}</span>
                <span class="rel-type">{r.rel_type.replaceAll("_", " ")}</span>
              </button>
            {/each}
          </div>
        {/if}

        <div class="links">
          <a class="link" href={entityHref(currentId)}>Full entry &rarr;</a>
          <a
            class="link"
            href={`${exploreHref()}?focus=${encodeURIComponent(currentId)}`}
          >
            Open in Explore &rarr;
          </a>
        </div>
      </div>

      <div class="right" aria-hidden="true">
        <MiniConstellation
          nodes={egoNodes}
          edges={egoEdges}
          focusId={currentId}
          revealedIds={null}
        />
      </div>
    </div>
  {/if}
</div>

<style>
  .codex {
    padding: 18px 20px 22px;
  }

  .cols {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 24px;
  }

  .left {
    min-width: 0;
  }

  .type-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .sw {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
  }

  .eyebrow {
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .name {
    font-family: var(--grim-serif);
    font-size: 20px;
    margin: 6px 0 0;
    color: var(--grim-ink);
  }

  .summary {
    margin: 8px 0 0;
    font-size: 13.5px;
    line-height: 1.5;
    color: var(--grim-text-dim);
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .pills {
    margin-top: 14px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line-soft);
    border-radius: 999px;
    padding: 5px 10px;
    font-family: inherit;
    font-size: 12px;
    color: var(--grim-text-dim);
  }

  .pill:hover {
    color: var(--grim-ink);
    border-color: var(--grim-accent);
  }

  .peer-name {
    font-family: var(--grim-serif);
    color: var(--grim-ink);
  }

  .rel-type {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--grim-text-faint);
  }

  .links {
    margin-top: 16px;
    display: flex;
    gap: 18px;
  }

  .link {
    font-size: 12.5px;
    font-weight: 600;
    text-decoration: none;
    color: var(--grim-accent);
  }

  .link:hover {
    text-decoration: underline;
  }

  .right {
    min-height: 240px;
    border-left: 1px solid var(--grim-line-soft);
    padding-left: 20px;
  }

  .status-error {
    color: var(--grim-type-creature);
    margin: 0;
  }

  .skeleton {
    height: 220px;
    border-radius: 8px;
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

  @media (prefers-reduced-motion: reduce) {
    .skeleton {
      animation: none;
    }
  }

  @media (max-width: 720px) {
    .cols {
      grid-template-columns: 1fr;
    }

    .right {
      border-left: 0;
      padding-left: 0;
      border-top: 1px solid var(--grim-line-soft);
      padding-top: 16px;
      min-height: 200px;
    }
  }
</style>
