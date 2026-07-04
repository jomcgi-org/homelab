<script>
  import { getContext } from "svelte";
  import { apiFetch, asQuery, entityHref } from "$lib/grimoire/api.js";

  const ctx = getContext("grimoire");

  const ENTITY_TYPES = [
    "creature",
    "spell",
    "location",
    "npc",
    "faction",
    "deity",
    "item",
  ];

  let typeFilter = $state("");
  let items = $state([]);
  let total = $state(0);
  let nextCursor = $state(null);
  let loading = $state(true);
  let loadingMore = $state(false);
  let error = $state("");

  // Reload whenever campaign, viewpoint, or the type filter changes.
  $effect(() => {
    load(ctx.campaignId, ctx.viewpoint, typeFilter);
  });

  async function load(campaignId, viewpoint, type) {
    loading = true;
    error = "";
    items = [];
    nextCursor = null;
    try {
      const extra = { limit: "60" };
      if (type) extra.type = type;
      const body = await apiFetch(
        `/campaigns/${campaignId}/entities?${asQuery(viewpoint, extra)}`,
      );
      items = body.items ?? [];
      total = body.total ?? items.length;
      nextCursor = body.next_cursor ?? null;
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  async function loadMore() {
    if (!nextCursor) return;
    loadingMore = true;
    try {
      const extra = { limit: "60", cursor: nextCursor };
      if (typeFilter) extra.type = typeFilter;
      const body = await apiFetch(
        `/campaigns/${ctx.campaignId}/entities?${asQuery(ctx.viewpoint, extra)}`,
      );
      items = [...items, ...(body.items ?? [])];
      nextCursor = body.next_cursor ?? null;
    } catch (e) {
      error = e.message;
    } finally {
      loadingMore = false;
    }
  }

  const isDm = $derived(ctx.viewpoint === "dm");
  const viewerName = $derived(
    ctx.characters.find((c) => c.id === ctx.viewpoint)?.character_name ?? "you",
  );

  function grantBadge(entity) {
    if (isDm) {
      const n = entity.grants?.length ?? 0;
      return n > 0 ? `${n} grant${n === 1 ? "" : "s"}` : "";
    }
    // A player's partial reveal has revealed_details but no full spine.
    return "revealed_details" in entity && !("source_type" in entity)
      ? "partial"
      : "";
  }
</script>

<div class="entities">
  <div class="filters">
    <button
      class="chip"
      class:chip--active={typeFilter === ""}
      onclick={() => (typeFilter = "")}>all</button
    >
    {#each ENTITY_TYPES as t (t)}
      <button
        class="chip"
        class:chip--active={typeFilter === t}
        onclick={() => (typeFilter = t)}>{t}</button
      >
    {/each}
    {#if !loading && !error}
      <span class="count">{total} total</span>
    {/if}
  </div>

  {#if loading}
    <ul class="grid">
      {#each Array(8) as _, i (i)}
        <li class="skeleton"></li>
      {/each}
    </ul>
  {:else if error}
    <p class="status status--error">{error}</p>
  {:else if items.length === 0}
    <div class="empty">
      {#if isDm}
        <p class="empty-lead">No entities yet.</p>
        <p class="empty-help">
          Entities appear as the extraction pass mines the loaded books.
        </p>
      {:else}
        <p class="empty-lead">
          The DM has not revealed anything to {viewerName} yet.
        </p>
        <p class="empty-help">What you learn at the table will show up here.</p>
      {/if}
    </div>
  {:else}
    <ul class="grid">
      {#each items as entity (entity.id)}
        <li>
          <a
            class="card"
            href={entityHref(ctx.campaignId, entity.id, ctx.viewpoint)}
          >
            <span class="card-name grim-title">{entity.name}</span>
            <span class="card-meta">
              <span class="card-type">{entity.entity_type}</span>
              {#if grantBadge(entity)}
                <span class="badge">{grantBadge(entity)}</span>
              {/if}
            </span>
          </a>
        </li>
      {/each}
    </ul>

    {#if nextCursor}
      <button class="more" disabled={loadingMore} onclick={loadMore}>
        {loadingMore ? "loading…" : "Load more"}
      </button>
    {/if}
  {/if}
</div>

<style>
  .entities {
    padding: clamp(1rem, 4vw, 2rem);
    max-width: 64rem;
    margin: 0 auto;
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: center;
    margin-bottom: 1.25rem;
  }

  .chip {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    min-height: 2.25rem;
    padding: 0.3rem 0.7rem;
    background: var(--bg);
    color: var(--fg-secondary);
    border: var(--border-thin);
    cursor: pointer;
  }

  .chip--active {
    background: var(--grim-accent);
    border-color: var(--grim-accent);
    color: #fff;
  }

  .count {
    margin-left: auto;
    font-size: 0.68rem;
    color: var(--fg-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
    gap: 0.6rem;
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    min-height: 3.5rem;
    padding: 0.75rem 0.85rem;
    border: 1px solid var(--grim-paper-line);
    background: var(--bg);
    color: var(--fg);
  }

  .card:hover {
    border-color: var(--grim-accent);
  }

  .card-name {
    font-size: 1.02rem;
  }

  .card-meta {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .card-type {
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-tertiary);
  }

  .badge {
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.08rem 0.35rem;
    border: var(--border-thin);
    color: var(--fg-secondary);
  }

  .more {
    display: block;
    margin: 1.25rem auto 0;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    min-height: 2.75rem;
    padding: 0.5rem 1.5rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
    cursor: pointer;
  }

  .more:hover {
    border-color: var(--grim-accent);
  }

  .empty {
    padding: 3rem 1rem;
    text-align: center;
  }

  .empty-lead {
    font-family: var(--grim-serif);
    font-size: 1.25rem;
    margin-bottom: 0.5rem;
  }

  .empty-help {
    font-size: 0.82rem;
    color: var(--fg-secondary);
  }

  .status--error {
    color: var(--danger);
    font-size: 0.8rem;
  }

  .skeleton {
    height: 3.9rem;
    border: 1px solid var(--grim-paper-line);
    background: linear-gradient(
      90deg,
      var(--surface) 25%,
      transparent 37%,
      var(--surface) 63%
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
