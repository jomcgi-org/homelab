<script>
  // World header search: a debounced typeahead over GET /entities?q=, plus the
  // old entities-page type filter chips that scope the search's `type` param.
  // Results are grouped by entity_type with a --grim-type-* colored dot;
  // selecting a result (mouse or keyboard) reports it up via `onselect(id)` so
  // the World page focuses that entity. Purely a control: it owns its own
  // fetching and dropdown state, never the graph.
  import { onDestroy } from "svelte";
  import { listEntities } from "$lib/public/grimoire/api.js";

  let { onselect = null } = $props();

  // The 10 entity types theme.css assigns a --grim-type-* hue to (the old
  // entities-page chip set). Other backend types still appear in results, just
  // grouped under a faint fallback dot; adding chips for them is a theme
  // change and out of scope.
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

  function typeVar(entityType) {
    return KNOWN_TYPES.has(entityType)
      ? `var(--grim-type-${entityType})`
      : "var(--grim-text-faint)";
  }

  function typeLabel(entityType) {
    return entityType.replace(/_/g, " ");
  }

  let q = $state("");
  let type = $state("");
  let items = $state([]);
  let open = $state(false);
  let loading = $state(false);
  // Active option index into the FLAT results list for keyboard nav; -1 = none.
  let activeIdx = $state(-1);

  let inputEl;
  let debounceTimer = null;
  let inFlight = null; // AbortController for the current request

  onDestroy(() => {
    clearTimeout(debounceTimer);
    inFlight?.abort();
  });

  // Group the flat results by entity_type for display, but keep a parallel
  // flat list (in group order) so arrow-key nav has a single linear index that
  // matches what the user sees top-to-bottom.
  const groups = $derived.by(() => {
    const by = new Map();
    for (const it of items) {
      if (!by.has(it.entity_type)) by.set(it.entity_type, []);
      by.get(it.entity_type).push(it);
    }
    return [...by.entries()].map(([entity_type, rows]) => ({
      entity_type,
      rows,
    }));
  });
  const flat = $derived(groups.flatMap((g) => g.rows));

  function scheduleSearch() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runSearch, 200);
  }

  async function runSearch() {
    // An empty query with no type chip is not a useful dropdown (it would list
    // the whole degree-ordered corpus); require either some text or a type.
    if (!q.trim() && !type) {
      items = [];
      open = false;
      activeIdx = -1;
      return;
    }
    inFlight?.abort();
    const ctrl = new AbortController();
    inFlight = ctrl;
    loading = true;
    try {
      const res = await listEntities({
        q,
        type,
        limit: 24,
        signal: ctrl.signal,
      });
      if (ctrl.signal.aborted) return;
      items = res.items ?? [];
      open = true;
      activeIdx = items.length ? 0 : -1;
    } catch {
      // Aborted or failed: leave the last results in place rather than
      // flickering the dropdown empty on every superseded keystroke.
    } finally {
      if (inFlight === ctrl) {
        inFlight = null;
        loading = false;
      }
    }
  }

  function onInput() {
    scheduleSearch();
  }

  function onFocus() {
    if (flat.length) open = true;
  }

  function selectChip(value) {
    type = type === value ? "" : value;
    runSearch();
    inputEl?.focus();
  }

  function choose(item) {
    if (!item) return;
    open = false;
    activeIdx = -1;
    onselect?.(item.id);
  }

  function onKeydown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open && flat.length) {
        open = true;
        return;
      }
      if (flat.length) activeIdx = (activeIdx + 1) % flat.length;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (flat.length) activeIdx = (activeIdx - 1 + flat.length) % flat.length;
    } else if (e.key === "Enter") {
      if (open && activeIdx >= 0 && flat[activeIdx]) {
        e.preventDefault();
        choose(flat[activeIdx]);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        open = false;
        activeIdx = -1;
      }
    }
  }

  // Close the dropdown when focus leaves the whole control (input or a result
  // row), not on every input blur (a mousedown on a result row would otherwise
  // close it before the click lands). Uses relatedTarget containment.
  let rootEl;
  function onFocusOut(e) {
    if (rootEl && !rootEl.contains(e.relatedTarget)) {
      open = false;
      activeIdx = -1;
    }
  }

  function optionId(i) {
    return `world-search-opt-${i}`;
  }
</script>

<div class="entity-search" bind:this={rootEl} onfocusout={onFocusOut}>
  <div class="search-box">
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
      bind:this={inputEl}
      class="search-input"
      type="text"
      placeholder="Search people and places..."
      autocomplete="off"
      role="combobox"
      aria-expanded={open}
      aria-controls="world-search-listbox"
      aria-activedescendant={open && activeIdx >= 0
        ? optionId(activeIdx)
        : null}
      aria-autocomplete="list"
      aria-label="Search people and places"
      bind:value={q}
      oninput={onInput}
      onfocus={onFocus}
      onkeydown={onKeydown}
    />
    {#if loading}<span class="spin" aria-hidden="true"></span>{/if}

    {#if open && flat.length}
      <ul
        class="results"
        role="listbox"
        id="world-search-listbox"
        aria-label="Search results"
      >
        {#each groups as g (g.entity_type)}
          <li class="group-head" role="presentation">
            {typeLabel(g.entity_type)}
          </li>
          {#each g.rows as it (it.id)}
            {@const idx = flat.indexOf(it)}
            <li
              class="result"
              class:active={idx === activeIdx}
              id={optionId(idx)}
              role="option"
              aria-selected={idx === activeIdx}
              tabindex="-1"
              onmousedown={(e) => {
                e.preventDefault();
                choose(it);
              }}
              onmousemove={() => (activeIdx = idx)}
            >
              <span
                class="dot"
                style={`background: ${typeVar(it.entity_type)}`}
                aria-hidden="true"
              ></span>
              <span class="result-name">{it.name}</span>
            </li>
          {/each}
        {/each}
      </ul>
    {/if}
  </div>

  <div
    class="chips"
    role="group"
    aria-label="Filter search by people and place type"
  >
    {#each TYPE_CHIPS as t (t.value)}
      <button
        type="button"
        class="chip"
        class:on={type === t.value}
        aria-pressed={type === t.value}
        onclick={() => selectChip(t.value)}
      >
        <span class="sw" style={`background: var(--grim-type-${t.value})`}
        ></span>
        {t.label}
      </button>
    {/each}
  </div>
</div>

<style>
  .entity-search {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }

  .search-box {
    position: relative;
  }

  .search-icon {
    position: absolute;
    left: 13px;
    top: 50%;
    transform: translateY(-50%);
    width: 16px;
    height: 16px;
    color: var(--grim-text-faint);
    pointer-events: none;
  }

  .search-input {
    width: 100%;
    height: 42px;
    padding: 0 34px 0 38px;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    font-size: 15px;
    font-family: inherit;
    color: var(--grim-ink);
    outline: none;
  }

  .search-input:focus-visible {
    border-color: var(--grim-accent);
    box-shadow: 0 0 0 3px var(--grim-accent-soft);
  }

  .spin {
    position: absolute;
    right: 13px;
    top: 50%;
    width: 14px;
    height: 14px;
    margin-top: -7px;
    border: 2px solid var(--grim-line);
    border-top-color: var(--grim-accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .spin {
      animation: none;
    }
  }

  .results {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    right: 0;
    z-index: 30;
    margin: 0;
    padding: 6px;
    list-style: none;
    max-height: min(50vh, 360px);
    overflow-y: auto;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(20, 30, 50, 0.16);
  }

  .group-head {
    font-family: var(--font-mono);
    font-size: 9.5px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    padding: 8px 8px 4px;
  }

  .result {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 7px 8px;
    border-radius: 7px;
    cursor: pointer;
    color: var(--grim-ink);
  }

  .result.active {
    background: var(--grim-accent-soft);
  }

  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
  }

  .result-name {
    font-family: var(--grim-serif);
    font-size: 14.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    padding: 5px 11px 5px 9px;
    font-size: 12px;
    font-family: inherit;
    color: var(--grim-text-dim);
    min-height: 30px;
  }

  .chip:hover {
    color: var(--grim-ink);
  }

  .chip .sw {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
  }

  .chip.on {
    color: var(--grim-ink);
    border-color: color-mix(in srgb, var(--grim-accent) 45%, var(--grim-line));
    background: var(--grim-accent-soft);
  }
</style>
