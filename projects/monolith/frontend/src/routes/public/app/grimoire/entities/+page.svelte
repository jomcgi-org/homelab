<script>
  // Public entity index: type filter + name search over the whole corpus (no
  // campaign/grants -- everything is visible). Results page via next_cursor.
  import { apiFetch, libraryHref, entityHref } from "$lib/public/grimoire/api.js";
  import BrutalistSelect from "$lib/public/components/BrutalistSelect.svelte";

  const TYPE_OPTIONS = [
    { value: "", label: "All types" },
    { value: "creature", label: "Creature" },
    { value: "spell", label: "Spell" },
    { value: "location", label: "Location" },
    { value: "npc", label: "NPC" },
    { value: "faction", label: "Faction" },
    { value: "deity", label: "Deity" },
    { value: "item", label: "Item" },
  ];

  let type = $state("");
  let q = $state("");
  let items = $state([]);
  let total = $state(0);
  let nextCursor = $state(null);
  let loading = $state(true);
  let loadingMore = $state(false);
  let error = $state("");

  const LIMIT = 40;

  // Debounce the search box so every keystroke doesn't fire a fetch; type
  // changes (via BrutalistSelect) reload immediately through the $effect below.
  let searchTimer = null;
  function onSearchInput() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(), 300);
  }

  $effect(() => {
    // Track `type` so switching the filter reloads from scratch. `q` is
    // intentionally excluded here -- it reloads via the debounced input
    // handler above, not on every render.
    void type;
    load();
  });

  function buildQuery(cursor) {
    const params = new URLSearchParams();
    if (type) params.set("type", type);
    if (q.trim()) params.set("q", q.trim());
    params.set("limit", String(LIMIT));
    if (cursor) params.set("cursor", cursor);
    return params.toString();
  }

  async function load() {
    loading = true;
    error = "";
    items = [];
    total = 0;
    nextCursor = null;
    try {
      const res = await apiFetch(`/entities?${buildQuery()}`);
      items = res.items ?? [];
      total = res.total ?? items.length;
      nextCursor = res.next_cursor ?? null;
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    loadingMore = true;
    try {
      const res = await apiFetch(`/entities?${buildQuery(nextCursor)}`);
      items = [...items, ...(res.items ?? [])];
      nextCursor = res.next_cursor ?? null;
    } catch (e) {
      error = e.message;
    } finally {
      loadingMore = false;
    }
  }

  // Secondary line from the list payload: creature -> size/CR, spell ->
  // level/school, everything else -> nothing extra beyond entity_type.
  function secondaryLine(ent) {
    if (ent.entity_type === "creature") {
      const parts = [];
      if (ent.size) parts.push(ent.size);
      if (ent.cr != null) parts.push(`CR ${ent.cr}`);
      return parts.join(" · ");
    }
    if (ent.entity_type === "spell") {
      const parts = [];
      parts.push(
        ent.level === 0 || ent.level === "0" ? "Cantrip" : `Level ${ent.level}`,
      );
      if (ent.school) parts.push(ent.school);
      return parts.join(" · ");
    }
    return "";
  }
</script>

<div class="wrap entities-page page">
  <a class="eyebrow back-link" href={libraryHref()}>&larr; LIBRARY</a>
  <h1 class="display page-title">Entities</h1>
  <p class="eyebrow count-line">
    {#if !loading}{total} indexed{/if}
  </p>

  <div class="filters">
    <div class="filter-select">
      <BrutalistSelect
        options={TYPE_OPTIONS}
        bind:value={type}
        label="Filter by type"
        id="entities-type"
      />
    </div>
    <input
      class="mono search-input"
      type="search"
      placeholder="Search by name..."
      bind:value={q}
      oninput={onSearchInput}
    />
  </div>

  {#if loading}
    <div class="grid">
      {#each Array(8) as _, i (i)}
        <div class="card-hard skeleton-card"></div>
      {/each}
    </div>
  {:else if error}
    <p class="mono status-error">{error}</p>
  {:else if items.length === 0}
    <div class="empty">
      <p class="empty-lead display">Nothing found.</p>
      <p class="empty-help">Try a different type or search term.</p>
    </div>
  {:else}
    <div class="grid">
      {#each items as ent (ent.id)}
        <a class="card-hard entity-card" href={entityHref(ent.id)}>
          <p class="display entity-name">{ent.name}</p>
          <p class="eyebrow entity-type">{ent.entity_type}</p>
          {#if secondaryLine(ent)}
            <p class="mono entity-secondary">{secondaryLine(ent)}</p>
          {/if}
        </a>
      {/each}
    </div>

    {#if nextCursor}
      <div class="load-more-row">
        <button class="btn btn-secondary" onclick={loadMore} disabled={loadingMore}>
          {loadingMore ? "LOADING..." : "LOAD MORE"}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .entities-page {
    padding: 48px 32px 96px;
  }

  .back-link {
    display: inline-block;
    margin-bottom: 20px;
  }

  .back-link:hover {
    color: var(--ink);
  }

  .page-title {
    font-size: clamp(32px, 6vw, 48px);
  }

  .count-line {
    margin-top: 8px;
    min-height: 14px;
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 28px;
    margin-bottom: 32px;
  }

  .filter-select {
    width: 220px;
    max-width: 100%;
  }

  .search-input {
    flex: 1;
    min-width: 200px;
    min-height: 44px;
    font-size: 13px;
    padding: 7px 12px;
    background: var(--cream);
    border: 2px solid var(--ink);
    color: var(--ink);
  }

  .search-input:focus-visible {
    background: var(--paper);
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
  }

  .entity-card {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 18px 20px;
    min-height: 44px;
  }

  .entity-name {
    font-size: 22px;
  }

  .entity-type {
    color: var(--ink-3);
  }

  .entity-secondary {
    color: var(--ink-2);
    font-size: 12px;
  }

  .skeleton-card {
    height: 96px;
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

  .load-more-row {
    display: flex;
    justify-content: center;
    margin-top: 32px;
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-card {
      animation: none;
    }
  }
</style>
