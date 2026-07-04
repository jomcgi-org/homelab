<script>
  import { goto } from "$app/navigation";
  import {
    apiFetch,
    asQuery,
    entityHref,
    chunkHref,
  } from "$lib/grimoire/api.js";

  let { campaignId, viewpoint } = $props();

  let query = $state("");
  let open = $state(false);
  let activeIndex = $state(-1);

  let nameHits = $state([]);
  let semanticEntities = $state([]);
  let loreHits = $state([]);

  let nameTimer;
  let semanticTimer;

  // One flat, ordered list so a single active index can move across all groups.
  const flat = $derived([
    ...nameHits.map((e) => ({ kind: "entity", entity: e, group: "Entities" })),
    ...semanticEntities.map((e) => ({
      kind: "entity",
      entity: e,
      group: "Related entities",
    })),
    ...loreHits.map((c) => ({ kind: "chunk", chunk: c, group: "Lore" })),
  ]);

  function reset() {
    nameHits = [];
    semanticEntities = [];
    loreHits = [];
    activeIndex = -1;
  }

  // A response is stale once the query has moved on; discard it rather than
  // clobbering results for the newer query (network completion order is not
  // guaranteed, so a slow "a" can land after "ab").
  const isStale = (q) => q !== query.trim();

  async function runNameSearch(q) {
    try {
      const body = await apiFetch(
        `/campaigns/${campaignId}/entities?${asQuery(viewpoint, {
          q,
          limit: "6",
        })}`,
      );
      if (isStale(q)) return;
      nameHits = body.items ?? [];
    } catch {
      if (!isStale(q)) nameHits = [];
    }
  }

  async function runSemanticSearch(q) {
    try {
      const hits = await apiFetch(
        `/campaigns/${campaignId}/search?${asQuery(viewpoint, { q, k: "8" })}`,
      );
      if (isStale(q)) return;
      const nameIds = new Set(nameHits.map((e) => e.id));
      semanticEntities = hits.filter(
        (h) => h.kind === "entity" && !nameIds.has(h.id),
      );
      loreHits = hits.filter((h) => h.kind === "chunk");
    } catch {
      if (!isStale(q)) {
        semanticEntities = [];
        loreHits = [];
      }
    }
  }

  // Instant name matches (150ms) render first; semantic results (300ms) fill in.
  $effect(() => {
    const q = query.trim();
    clearTimeout(nameTimer);
    clearTimeout(semanticTimer);
    if (!q) {
      reset();
      return;
    }
    open = true;
    nameTimer = setTimeout(() => runNameSearch(q), 150);
    semanticTimer = setTimeout(() => runSemanticSearch(q), 300);
    return () => {
      clearTimeout(nameTimer);
      clearTimeout(semanticTimer);
    };
  });

  function choose(item) {
    open = false;
    query = "";
    reset();
    if (item.kind === "entity") {
      goto(entityHref(campaignId, item.entity.id, viewpoint));
    } else {
      goto(
        chunkHref(campaignId, item.chunk.book_id, item.chunk.id, viewpoint),
      );
    }
  }

  function onKeydown(e) {
    if (e.key === "Escape") {
      open = false;
      activeIndex = -1;
      return;
    }
    if (!flat.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % flat.length;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex - 1 + flat.length) % flat.length;
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      choose(flat[activeIndex]);
    }
  }

  // Group headers: emit a header row before the first item of each group.
  const rows = $derived(
    flat.map((item, i) => ({
      item,
      i,
      header: i === 0 || flat[i - 1].group !== item.group ? item.group : null,
    })),
  );
</script>

<div class="omnibox" onfocusout={(e) => {
  if (!e.currentTarget.contains(e.relatedTarget)) open = false;
}}>
  <input
    class="ob-input"
    type="search"
    placeholder="Search entities and lore…"
    bind:value={query}
    onfocus={() => query.trim() && (open = true)}
    onkeydown={onKeydown}
    role="combobox"
    aria-expanded={open}
    aria-controls="ob-results"
    aria-autocomplete="list"
  />

  {#if open && query.trim()}
    <div class="ob-panel" id="ob-results" role="listbox">
      {#if flat.length === 0}
        <p class="ob-empty">No matches yet…</p>
      {:else}
        {#each rows as row (row.i)}
          {#if row.header}
            <div class="ob-group">{row.header}</div>
          {/if}
          <button
            class="ob-hit"
            class:ob-hit--active={activeIndex === row.i}
            role="option"
            aria-selected={activeIndex === row.i}
            onmouseenter={() => (activeIndex = row.i)}
            onclick={() => choose(row.item)}
          >
            {#if row.item.kind === "entity"}
              <span class="ob-title">{row.item.entity.name}</span>
              <span class="ob-type">{row.item.entity.entity_type}</span>
            {:else}
              <span class="ob-title">{row.item.chunk.display_name}</span>
              <span class="ob-type">{row.item.chunk.section_path ?? "lore"}</span
              >
            {/if}
          </button>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  .omnibox {
    position: relative;
    width: 100%;
  }

  .ob-input {
    width: 100%;
    min-height: 2.5rem;
    font-family: var(--font-mono);
    font-size: 0.85rem;
    padding: 0.5rem 0.75rem;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-thin);
  }

  .ob-input:focus {
    outline: 2px solid var(--grim-accent);
    outline-offset: -2px;
  }

  .ob-panel {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: 40;
    max-height: 60vh;
    overflow-y: auto;
    background: var(--bg);
    /* Flat brutalist chrome: a heavier second border lifts the panel off the
     * page instead of a drop shadow (grimoire has no box-shadows anywhere). */
    border: var(--border-heavy);
    border-top-width: 3px;
  }

  .ob-empty {
    padding: 0.75rem;
    font-size: 0.78rem;
    color: var(--fg-tertiary);
  }

  .ob-group {
    padding: 0.4rem 0.75rem 0.2rem;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg-tertiary);
    background: var(--surface);
  }

  .ob-hit {
    width: 100%;
    text-align: left;
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    min-height: 2.5rem;
    padding: 0.45rem 0.75rem;
    background: none;
    border: none;
    border-top: 1px solid var(--surface);
    cursor: pointer;
    font-family: var(--font-mono);
    color: var(--fg);
  }

  .ob-hit--active {
    background: var(--surface);
  }

  .ob-title {
    font-weight: 700;
    font-size: 0.82rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .ob-type {
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-tertiary);
    margin-left: auto;
    white-space: nowrap;
  }
</style>
