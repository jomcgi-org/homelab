<script>
  // Dense, single-column entity index. Rendered two ways: as the left pane of
  // the desktop two-pane Shell, and full-width as the mobile /entities screen.
  // Owns its own type filter, loading, and pagination so neither host route has
  // to reinvent them.
  import { getContext } from "svelte";
  import { page } from "$app/stores";
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

  // The entity currently open in the reading pane (so the list can mark it).
  const activeId = $derived($page.params.id ?? "");

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

<div class="index">
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
      <span class="count">{total}</span>
    {/if}
  </div>

  {#if loading}
    <ul class="rows">
      {#each Array(10) as _, i (i)}
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
    <ul class="rows">
      {#each items as entity (entity.id)}
        <li>
          <a
            class="row"
            class:row--active={entity.id === activeId}
            href={entityHref(ctx.campaignId, entity.id, ctx.viewpoint)}
          >
            <span class="row-name grim-title">{entity.name}</span>
            <span class="row-tail">
              <span class="row-type">{entity.entity_type}</span>
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
  .index {
    padding: 0.75rem;
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .chip {
    font-family: var(--font-mono);
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    min-height: 2.25rem;
    padding: 0.25rem 0.55rem;
    background: var(--grim-surface);
    color: var(--grim-text-dim);
    border: 1px solid var(--grim-line);
    cursor: pointer;
  }

  .chip--active {
    background: var(--grim-accent);
    border-color: var(--grim-accent);
    color: var(--grim-on-accent);
  }

  .count {
    margin-left: auto;
    font-size: 0.66rem;
    color: var(--grim-text-faint);
    font-variant-numeric: tabular-nums;
  }

  .rows {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    min-height: 2.75rem;
    padding: 0.4rem 0.6rem;
    border: 1px solid transparent;
    border-left: 2px solid transparent;
    background: var(--grim-surface);
    color: var(--grim-ink);
  }

  .row:hover {
    border-color: var(--grim-paper-line);
    border-left-color: var(--grim-accent);
  }

  .row--active {
    border-color: var(--grim-paper-line);
    border-left-color: var(--grim-accent);
    background: var(--grim-surface-2);
  }

  .row-name {
    font-size: 0.95rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-tail {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-shrink: 0;
  }

  .row-type {
    font-size: 0.58rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-text-faint);
  }

  .badge {
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.08rem 0.35rem;
    border: 1px solid var(--grim-line);
    color: var(--grim-text-dim);
    white-space: nowrap;
  }

  .more {
    display: block;
    margin: 0.75rem auto 0;
    font-family: var(--font-mono);
    font-size: 0.72rem;
    min-height: 2.75rem;
    padding: 0.4rem 1.25rem;
    background: var(--grim-surface);
    color: var(--grim-ink);
    border: 1px solid var(--grim-line);
    cursor: pointer;
  }

  .more:hover {
    border-color: var(--grim-accent);
  }

  .empty {
    padding: 2.5rem 1rem;
    text-align: center;
  }

  .empty-lead {
    font-family: var(--grim-serif);
    font-size: 1.1rem;
    margin-bottom: 0.5rem;
  }

  .empty-help {
    font-size: 0.78rem;
    color: var(--grim-text-dim);
  }

  .status--error {
    color: var(--grim-type-creature);
    font-size: 0.8rem;
  }

  .skeleton {
    height: 2.75rem;
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
