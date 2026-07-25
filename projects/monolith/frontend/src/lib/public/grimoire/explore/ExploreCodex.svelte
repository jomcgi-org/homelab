<script>
  // Slide-in EXPLORE codex: the detail panel that opens over the canvas when a
  // node is selected. Reuses EntityDetail (creature/spell/generic statblock
  // dispatch, same as the entity detail page) for the "summary + statblock"
  // body, then adds two EXPLORE-specific sections below it:
  //   - Relationships, grouped by rel_type, sourced from /explore/ego (not
  //     /entities/{id}/relationships): ego already returns the neighbor node
  //     projections (entity_type, etc.) the codex needs for the color swatch,
  //     and it's the same payload the canvas would use to expand a "guest"
  //     node, so one endpoint serves both jobs.
  //   - "Appears in the books", from /entities/{id}/mentions, linking into the
  //     public reader.
  //
  // Contract: `entityId` in, `onselect(peerId)` out when a relationship row is
  // clicked (parent decides whether that peer needs pulling in as a canvas
  // guest), `onclose()` out for the close button. This component owns its own
  // fetching (keyed on `entityId`); it does not know about scope/lens/guests.
  import {
    API,
    apiFetch,
    exploreEgo,
    chunkHref,
  } from "$lib/public/grimoire/api.js";
  import EntityDetail from "$lib/public/grimoire/statblock/EntityDetail.svelte";
  import { phrase } from "$lib/public/grimoire/world/relationship-phrases.js";

  let { entityId = null, onselect = null, onclose = null } = $props();

  let entity = $state(null);
  let relationships = $state([]);
  let mentions = $state([]);
  let loading = $state(false);
  let error = $state("");

  $effect(() => {
    const id = entityId;
    if (!id) return;
    load(id);
  });

  async function load(id) {
    loading = true;
    error = "";
    entity = null;
    relationships = [];
    mentions = [];
    try {
      entity = await apiFetch(`/entities/${encodeURIComponent(id)}`);
    } catch (e) {
      error = e.message;
      loading = false;
      return;
    }
    // Relationships + mentions are best-effort: a failure in either must not
    // blank the statblock that already loaded.
    const [egoRes, mentionsRes] = await Promise.allSettled([
      exploreEgo(id),
      apiFetch(`/entities/${encodeURIComponent(id)}/mentions`),
    ]);
    relationships =
      egoRes.status === "fulfilled" ? buildRelationships(id, egoRes.value) : [];
    mentions =
      mentionsRes.status === "fulfilled" ? (mentionsRes.value ?? []) : [];
    loading = false;
  }

  // Ego edges are undirected pairs {from, to, rel_type}; resolve each into
  // "the other entity" plus the raw edge (so the phrase builder can word it by
  // direction relative to the focus), using ego's own node list (no extra
  // fetch needed for the peer's name/type).
  function buildRelationships(id, ego) {
    const nodesById = new Map((ego?.nodes ?? []).map((n) => [n.id, n]));
    return (ego?.edges ?? [])
      .map((e) => {
        const out = e.from === id;
        const peer = nodesById.get(out ? e.to : e.from);
        if (!peer) return null;
        return { rel_type: e.rel_type, peer, edge: e };
      })
      .filter(Boolean)
      .sort(
        (a, b) =>
          a.rel_type.localeCompare(b.rel_type) ||
          a.peer.name.localeCompare(b.peer.name),
      );
  }

  const groups = $derived.by(() => {
    const by = new Map();
    for (const r of relationships) {
      if (!by.has(r.rel_type)) by.set(r.rel_type, []);
      by.get(r.rel_type).push(r);
    }
    return [...by.entries()].map(([rel_type, items]) => ({ rel_type, items }));
  });

  function relLabel(relType) {
    return relType.replaceAll("_", " ").toLowerCase();
  }

  function typeLabel(entityType) {
    return entityType.replaceAll("_", " ");
  }

  // entity_type is corpus-controlled; guard it before interpolating into a
  // CSS custom-property name (same pattern as ConstellationDock's typeVar).
  const TYPE_ALLOWLIST = /^[a-z_]+$/;

  function typeVar(entityType, fallback) {
    const type = TYPE_ALLOWLIST.test(entityType ?? "") ? entityType : "class";
    return `var(--grim-type-${type}, ${fallback})`;
  }

  function swatch(entityType) {
    return `background: ${typeVar(entityType, "var(--grim-text-faint)")}`;
  }

  // Entity art (when the focus carries an image_chunk_id, added in Task 1) is
  // the same chunk-image endpoint the library covers use. Absent an image we
  // fall back to a type-tinted monogram device, so every entity has a visual
  // anchor above its description.
  const artUrl = $derived(
    entity?.image_chunk_id
      ? `${API}/chunks/${encodeURIComponent(entity.image_chunk_id)}/image`
      : null,
  );

  const monogram = $derived(
    (entity?.name ?? "?").trim().charAt(0).toUpperCase() || "?",
  );

  // Build the reading phrase for one relationship row relative to the focus.
  function relPhrase(r) {
    return phrase({ focusId: entityId, edge: r.edge, peerName: r.peer.name });
  }
</script>

<aside
  class="codex"
  class:open={!!entityId}
  aria-hidden={!entityId}
  inert={!entityId}
>
  <button
    type="button"
    class="close"
    onclick={() => onclose?.()}
    aria-label="Close codex"
  >
    &times;
  </button>

  {#if entityId}
    {#if loading}
      <div class="skeleton" aria-hidden="true"></div>
    {:else if error}
      <p class="status-error">{error}</p>
    {:else if entity}
      <header class="codex-head">
        <div class="type-row">
          <span class="sw" style={swatch(entity.entity_type)}></span>
          <span class="eyebrow">{typeLabel(entity.entity_type)}</span>
        </div>
        <h2 class="codex-name grim-title">{entity.name}</h2>
      </header>

      {#if artUrl}
        <div class="art">
          <img
            src={artUrl}
            alt={`Illustration of ${entity.name}`}
            loading="lazy"
          />
        </div>
      {:else}
        <div
          class="monogram"
          style={`--mono: ${typeVar(entity.entity_type, "var(--grim-text-faint)")}`}
          aria-hidden="true"
        >
          {monogram}
        </div>
      {/if}

      <div class="detail-wrap">
        <EntityDetail {entity} />
      </div>

      {#if groups.length}
        <h3 class="eyebrow section-head">
          Relationships &middot; {relationships.length}
        </h3>
        <div class="rel-groups">
          {#each groups as g (g.rel_type)}
            <div class="rel-group">
              <p class="rel-type-label">{relLabel(g.rel_type)}</p>
              {#each g.items as r (r.peer.id + "|" + r.rel_type + "|" + r.edge.from + "|" + r.edge.to)}
                {@const p = relPhrase(r)}
                <p class="rel-line">
                  {#if p.pre}<span class="rel-word">{p.pre}</span>{/if}<button
                    type="button"
                    class="rel-peer"
                    style={`--peer: ${typeVar(r.peer.entity_type, "var(--grim-text-faint)")}`}
                    onclick={() => onselect?.(r.peer.id)}>{p.peer}</button
                  >{#if p.post}<span class="rel-word">{p.post}</span>{/if}
                </p>
              {/each}
            </div>
          {/each}
        </div>
      {/if}

      {#if mentions.length}
        <h3 class="eyebrow section-head">Appears in the books</h3>
        <ul class="mention-list">
          {#each mentions as m, i (i)}
            <li>
              <a class="mention" href={chunkHref(m.book_id, m.chunk_id)}>
                <span class="eyebrow mention-where"
                  >{m.section_path ?? "lore"}</span
                >
                <span class="mention-preview">{m.preview}</span>
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {/if}
</aside>

<style>
  .codex {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(360px, 92%);
    background: color-mix(in srgb, var(--grim-surface) 94%, transparent);
    backdrop-filter: blur(12px);
    border-left: 1px solid var(--grim-line);
    transform: translateX(100%);
    transition: transform 0.3s cubic-bezier(0.22, 0.61, 0.36, 1);
    padding: 24px 22px 32px;
    overflow-y: auto;
  }

  .codex.open {
    transform: translateX(0);
  }

  @media (prefers-reduced-motion: reduce) {
    .codex {
      transition: none;
    }
  }

  .close {
    position: absolute;
    top: 13px;
    right: 15px;
    background: none;
    border: 1px solid var(--grim-line);
    border-radius: 6px;
    width: 27px;
    height: 27px;
    cursor: pointer;
    color: var(--grim-text-dim);
    font-size: 15px;
    line-height: 1;
  }

  .close:hover {
    color: var(--grim-ink);
    border-color: var(--grim-accent);
  }

  .eyebrow {
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .codex-head {
    margin-top: 4px;
  }

  .type-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .codex-name {
    /* Compact, NOT the giant fill: the statblock's own name (hidden below) was
       the loud display head; the codex owns a quieter ~1.5rem serif line so
       the art and description carry the panel. */
    font-size: 1.5rem;
    line-height: 1.15;
    margin: 6px 0 0;
    color: var(--grim-ink);
  }

  .sw {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
  }

  .art {
    margin-top: 14px;
    aspect-ratio: 3 / 2;
    border-radius: 9px;
    overflow: hidden;
    border: 1px solid var(--grim-line-soft);
    background: var(--grim-surface-2);
  }

  .art img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  .monogram {
    margin-top: 14px;
    aspect-ratio: 3 / 2;
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--grim-serif);
    font-size: 4rem;
    font-weight: 600;
    color: var(--mono);
    background: color-mix(in srgb, var(--mono) 12%, var(--grim-surface-2));
    border: 1px solid color-mix(in srgb, var(--mono) 28%, var(--grim-line-soft));
  }

  .detail-wrap {
    margin-top: 14px;
  }

  /* EntityDetail's statblocks set their own max-width (40rem) for the wide
     entity-page layout; inside this ~340px rail that just means "fill the
     rail", which is what we want here. */
  .detail-wrap :global(article) {
    max-width: none;
    padding: 0;
    border: 0;
  }

  /* The codex renders its own compact name/type header above, so hide the
     statblock's loud display name + type strap (EntityDetail is shared with
     the full entity page, where those still show). */
  .detail-wrap :global(.name),
  .detail-wrap :global(.strap) {
    display: none;
  }

  .section-head {
    margin: 22px 0 8px;
    border-bottom: 1px solid var(--grim-line-soft);
    padding-bottom: 7px;
  }

  .rel-groups {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .rel-type-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    margin: 0 0 2px;
  }

  /* Relationships read as phrases: quiet body words with the peer name as a
     type-underlined inline link that refocuses. No arrows or glyphs. */
  .rel-line {
    margin: 0;
    padding: 5px 0;
    font-family: var(--grim-serif);
    font-size: 14.5px;
    line-height: 1.4;
    color: var(--grim-text-dim);
  }

  .rel-word {
    color: var(--grim-text-dim);
  }

  .rel-peer {
    background: none;
    border: 0;
    padding: 0;
    margin: 0;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
    color: var(--grim-ink);
    text-decoration: underline;
    text-decoration-color: var(--peer, var(--grim-line));
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
  }

  .rel-peer:hover,
  .rel-peer:focus-visible {
    color: var(--grim-accent);
  }

  .mention-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .mention {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 10px 12px;
    text-decoration: none;
    color: inherit;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line-soft);
    border-radius: 7px;
  }

  .mention:hover {
    background: var(--grim-surface-2);
    border-color: var(--grim-line);
  }

  .mention-preview {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: 12.5px;
    color: var(--grim-text-dim);
  }

  .status-error {
    color: var(--grim-type-creature);
    margin-top: 16px;
  }

  .skeleton {
    margin-top: 16px;
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
</style>
