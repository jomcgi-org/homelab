<script>
  // Public entity index: type filter + name search over the whole corpus (no
  // campaign/grants -- everything is visible). Results page via next_cursor.
  import { onMount } from "svelte";
  import { apiFetch, libraryHref, entityHref } from "$lib/public/grimoire/api.js";

  // Color-coded filter chips: only the 10 entity types theme.css assigns a
  // --grim-type-* hue to (the 7 lore types plus event/quest/class -- see the
  // token block in $lib/grimoire/theme.css). The backend's EntityType enum
  // also has ~9 "mechanics" types (condition, feat, race, background,
  // subclass, class_feature, action, rule, table); those still come back from
  // GET /entities and still render, just without a dedicated chip or hue (see
  // typeColorVar's fallback below) -- adding tokens for them is a theme.css
  // change and out of scope for this page-only restyle.
  const TYPE_CHIPS = [
    { value: "creature", label: "Creature" },
    { value: "spell", label: "Spell" },
    { value: "location", label: "Location" },
    { value: "npc", label: "NPC" },
    { value: "faction", label: "Faction" },
    { value: "deity", label: "Deity" },
    { value: "item", label: "Item" },
    { value: "event", label: "Event" },
    { value: "quest", label: "Quest" },
    { value: "class", label: "Class" },
  ];
  const KNOWN_TYPES = new Set(TYPE_CHIPS.map((t) => t.value));

  function typeColorVar(entityType) {
    return KNOWN_TYPES.has(entityType)
      ? `var(--grim-type-${entityType})`
      : "var(--grim-text-faint)";
  }

  function typeLabel(entityType) {
    return entityType.replace(/_/g, " ");
  }

  let type = $state("");
  let q = $state("");
  let items = $state([]);
  let total = $state(0);
  let nextCursor = $state(null);
  let loading = $state(true);
  let loadingMore = $state(false);
  let error = $state("");

  // Corpus-wide per-type counts for the chip badges. Fetched once on mount,
  // independent of the live search/type filter above (limit=1 -- only the
  // `total` field is read), so a chip's number always reads "how many of
  // this type exist", not "how many match the current search".
  let allCount = $state(null);
  let typeCounts = $state({});

  onMount(loadChipCounts);

  async function loadChipCounts() {
    try {
      const all = await apiFetch("/entities?limit=1");
      allCount = all.total ?? null;
    } catch {
      allCount = null;
    }
    await Promise.all(
      TYPE_CHIPS.map(async (t) => {
        try {
          const res = await apiFetch(
            `/entities?type=${encodeURIComponent(t.value)}&limit=1`,
          );
          typeCounts = { ...typeCounts, [t.value]: res.total ?? 0 };
        } catch {
          // Non-fatal: the chip just renders without a count badge.
        }
      }),
    );
  }

  const LIMIT = 40;

  // Debounce the search box so every keystroke doesn't fire a fetch; type
  // changes (via the chip row) reload immediately through the $effect below.
  let searchTimer = null;
  function onSearchInput() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => load(), 300);
  }

  function selectType(value) {
    type = type === value ? "" : value;
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

<div class="entities-page">
  <a class="eyebrow back-link" href={libraryHref()}>&larr; Library</a>
  <h1 class="grim-title page-title">Entities</h1>
  <p class="eyebrow count-line">
    {#if !loading}{total.toLocaleString()} indexed{/if}
  </p>

  <div class="controls">
    <div class="search">
      <svg
        class="search-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <circle cx="11" cy="11" r="7" />
        <path d="M21 21l-4.3-4.3" />
      </svg>
      <input
        class="search-input"
        type="search"
        placeholder="Search entities by name..."
        aria-label="Search entities"
        bind:value={q}
        oninput={onSearchInput}
      />
    </div>

    <div class="chips" role="group" aria-label="Filter by entity type">
      <button
        type="button"
        class="chip chip-all"
        class:on={type === ""}
        aria-pressed={type === ""}
        onclick={() => (type = "")}
      >
        All
        {#if allCount != null}<span class="n">{allCount.toLocaleString()}</span
          >{/if}
      </button>
      {#each TYPE_CHIPS as t (t.value)}
        <button
          type="button"
          class="chip"
          class:on={type === t.value}
          aria-pressed={type === t.value}
          onclick={() => selectType(t.value)}
        >
          <span class="sw" style={`background: var(--grim-type-${t.value})`}
          ></span>
          {t.label}
          {#if typeCounts[t.value] != null}<span class="n"
              >{typeCounts[t.value].toLocaleString()}</span
            >{/if}
        </button>
      {/each}
    </div>
  </div>

  {#if loading}
    <div class="grid">
      {#each Array(8) as _, i (i)}
        <div class="skeleton-card"></div>
      {/each}
    </div>
  {:else if error}
    <p class="status-error">{error}</p>
  {:else if items.length === 0}
    <div class="empty">
      <p class="grim-title empty-lead">Nothing found.</p>
      <p class="empty-help">Try a different type or search term.</p>
    </div>
  {:else}
    <div class="grid">
      {#each items as ent (ent.id)}
        <a
          class="ent"
          href={entityHref(ent.id)}
          style={`--ec: ${typeColorVar(ent.entity_type)}`}
        >
          <span class="ent-main">
            <span class="nm">{ent.name}</span>
            {#if secondaryLine(ent)}<span class="sec">{secondaryLine(ent)}</span
              >{/if}
          </span>
          <span class="ty">{typeLabel(ent.entity_type)}</span>
        </a>
      {/each}
    </div>

    {#if nextCursor}
      <div class="load-more-row">
        <button class="load-more" onclick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading..." : "Load more"}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .entities-page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 40px 28px 80px;
  }

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .back-link {
    display: inline-block;
    margin-bottom: 20px;
    text-decoration: none;
  }

  .back-link:hover {
    color: var(--grim-text-dim);
  }

  .page-title {
    font-size: clamp(32px, 6vw, 46px);
    margin: 6px 0 0;
  }

  .count-line {
    margin-top: 14px;
    min-height: 14px;
    font-variant-numeric: tabular-nums;
  }

  .controls {
    margin-top: 26px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .search {
    position: relative;
  }

  .search-icon {
    position: absolute;
    left: 14px;
    top: 50%;
    transform: translateY(-50%);
    width: 16px;
    height: 16px;
    color: var(--grim-text-faint);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    height: 44px;
    padding: 0 14px 0 40px;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    font-size: 15px;
    color: var(--grim-ink);
    outline: none;
  }

  .search-input:focus-visible {
    border-color: var(--grim-accent);
    box-shadow: 0 0 0 3px var(--grim-accent-soft);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    cursor: pointer;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 6px 13px 6px 10px;
    font-size: 12.5px;
    font-family: inherit;
    color: var(--grim-text-dim);
    min-height: 32px;
  }

  .chip:hover {
    color: var(--grim-ink);
  }

  .chip .sw {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
  }

  .chip .n {
    color: var(--grim-text-faint);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }

  .chip.on {
    color: var(--grim-ink);
    border-color: color-mix(in srgb, var(--grim-accent) 45%, var(--grim-line));
    background: var(--grim-accent-soft);
  }

  .chip-all.on {
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    border-color: var(--grim-accent);
  }

  .chip-all.on .n {
    color: inherit;
    opacity: 0.8;
  }

  .grid {
    margin-top: 22px;
    display: grid;
    gap: 8px;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }

  .ent {
    display: flex;
    align-items: center;
    gap: 12px;
    cursor: pointer;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line-soft);
    border-left: 3px solid var(--ec, var(--grim-text-faint));
    border-radius: 8px;
    padding: 11px 14px;
    text-decoration: none;
    color: inherit;
    transition:
      background 0.12s,
      border-color 0.12s;
  }

  .ent:hover {
    background: var(--grim-surface-2);
    border-color: var(--grim-line);
    border-left-color: var(--ec, var(--grim-text-faint));
  }

  .ent-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .nm {
    font-family: var(--grim-serif);
    font-size: 16px;
    line-height: 1.2;
    color: var(--grim-ink);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .sec {
    font-size: 11.5px;
    color: var(--grim-text-dim);
    font-variant-numeric: tabular-nums;
  }

  .ty {
    margin-left: auto;
    flex: none;
    font-size: 9.5px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
  }

  .skeleton-card {
    height: 64px;
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

  .empty {
    padding: 64px 0;
    text-align: center;
  }

  .empty-lead {
    font-size: 28px;
    margin-bottom: 8px;
  }

  .empty-help {
    color: var(--grim-text-dim);
  }

  .status-error {
    color: var(--grim-type-creature);
    padding: 32px 0;
  }

  .load-more-row {
    display: flex;
    justify-content: center;
    margin-top: 32px;
  }

  .load-more {
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    color: var(--grim-text-dim);
    padding: 11px 22px;
  }

  .load-more:hover {
    color: var(--grim-ink);
    border-color: var(--grim-accent);
  }

  .load-more:disabled {
    opacity: 0.6;
    cursor: default;
  }

  @media (max-width: 640px) {
    .entities-page {
      padding: 28px 20px 60px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-card {
      animation: none;
    }
  }
</style>
