<script>
  // Public EXPLORE: a scope selector (which slice of the corpus) crossed with
  // a lens (how to view that slice), rendered as a force-directed canvas with
  // a slide-in codex on selection. Scope/lens/focus are mirrored into the URL
  // query string (?scope=&lens=&focus=) so any view is a shareable link,
  // mirroring the notes app's view/focus URL sync (routes/public/app/notes/
  // +page.svelte's syncUrl) and the stars map's param sync.
  //
  // ssr is off for the whole public grimoire tree (see the root +layout.js
  // docblock): this page fetches everything client-side after the Turnstile
  // gate admits.
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import {
    exploreGraph,
    exploreEgo,
    listAllAdventures,
    libraryHref,
  } from "$lib/public/grimoire/api.js";
  import ExploreCanvas from "$lib/public/grimoire/explore/ExploreCanvas.svelte";
  import ExploreCodex from "$lib/public/grimoire/explore/ExploreCodex.svelte";

  const LENS_OPTIONS = [
    { value: "world", label: "World" },
    { value: "story", label: "Story" },
    { value: "quests", label: "Quests" },
    { value: "rules", label: "Rules" },
  ];
  const LENS_VALUES = new Set(LENS_OPTIONS.map((l) => l.value));

  let scope = $state("everything");
  let lens = $state("world");
  let focusId = $state(null);

  let adventures = $state([]);
  let adventuresLoading = $state(true);

  let baseGraph = $state({ nodes: [], edges: [] });
  let graphLoading = $state(true);
  let graphError = $state("");

  // Nodes/edges pulled in by following a relationship to a peer outside the
  // current scope/lens ("guest" nodes, dashed ring on the canvas). Cleared on
  // every scope/lens change -- switching the slice is a fresh view, not a
  // silent carry-over of whatever you'd wandered into.
  let guestNodesById = $state({});
  let guestEdgeMap = $state({});

  const baseIds = $derived(new Set(baseGraph.nodes.map((n) => n.id)));
  const baseEdgeKeys = $derived(
    new Set(baseGraph.edges.map((e) => `${e.from}|${e.to}|${e.rel_type}`)),
  );
  const knownIds = $derived(new Set([...baseIds, ...Object.keys(guestNodesById)]));
  const guestIdSet = $derived(new Set(Object.keys(guestNodesById)));
  const combinedNodes = $derived([
    ...baseGraph.nodes,
    ...Object.values(guestNodesById),
  ]);
  const combinedEdges = $derived([
    ...baseGraph.edges,
    ...Object.values(guestEdgeMap),
  ]);

  const legendTypes = $derived(
    [...new Set(combinedNodes.map((n) => n.entity_type))].sort(),
  );

  onMount(() => {
    bootstrap();
  });

  async function bootstrap() {
    const params = $page.url.searchParams;
    const urlLens = params.get("lens");
    lens = urlLens && LENS_VALUES.has(urlLens) ? urlLens : "world";
    focusId = params.get("focus") || null;
    const urlScope = params.get("scope");

    const advsPromise = loadAdventures();
    if (urlScope) {
      scope = urlScope;
    } else {
      // Default scope needs the adventure list resolved first: the first
      // adventure (book/seq order), or "everything" if none are loaded yet.
      const advs = await advsPromise;
      scope = advs.length ? `adventure:${advs[0].id}` : "everything";
    }
    await loadGraph();
  }

  async function loadAdventures() {
    adventuresLoading = true;
    try {
      adventures = await listAllAdventures();
    } catch {
      adventures = [];
    } finally {
      adventuresLoading = false;
    }
    return adventures;
  }

  async function loadGraph() {
    graphLoading = true;
    graphError = "";
    guestNodesById = {};
    guestEdgeMap = {};
    try {
      baseGraph = await exploreGraph(scope, lens);
    } catch (e) {
      graphError = e.message;
      baseGraph = { nodes: [], edges: [] };
    } finally {
      graphLoading = false;
    }
    // A focused entity that isn't part of the new scope/lens is cleared
    // rather than silently re-pulled in as a guest: switching the slice
    // should read as a fresh view.
    if (focusId && !baseGraph.nodes.some((n) => n.id === focusId)) {
      focusId = null;
    }
    syncUrl();
  }

  function onScopeChange(nextScope) {
    if (nextScope === scope) return;
    scope = nextScope;
    loadGraph();
  }

  function onLensChange(nextLens) {
    if (nextLens === lens) return;
    lens = nextLens;
    loadGraph();
  }

  // Selection can come from a canvas click, a codex relationship row, or (via
  // bootstrap) a deep-linked ?focus=. `id` is null for "click empty canvas" /
  // deselect.
  function handleSelect(id) {
    focusId = id;
    syncUrl();
  }

  function handleClose() {
    focusId = null;
    syncUrl();
  }

  // Whenever the focus is an entity outside the current base graph (a guest
  // candidate), pull its 1-hop neighborhood so the canvas has something to
  // draw at that id and the codex's relationship list has somewhere to route
  // "expand" clicks from. `egoAttempted` is a plain (non-reactive) memo, not
  // scope/lens-cleared: ego is lens-agnostic, so an id that fails to resolve
  // (bad deep link, private entity) will never resolve later either, and
  // without this guard `knownIds` changing for an unrelated guest would
  // re-fire this effect and retry the same dead id forever.
  const egoAttempted = new Set();
  $effect(() => {
    const id = focusId;
    if (!id || knownIds.has(id) || egoAttempted.has(id)) return;
    egoAttempted.add(id);
    loadGuestEgo(id);
  });

  async function loadGuestEgo(id) {
    try {
      const ego = await exploreEgo(id);
      mergeGuestEgo(ego);
    } catch {
      // Best-effort: the node just won't render on the canvas; the codex's
      // own /entities/{id} fetch still shows the entity's own detail.
    }
  }

  function mergeGuestEgo(ego) {
    const nextNodes = { ...guestNodesById };
    for (const n of ego.nodes ?? []) {
      if (!knownIds.has(n.id)) nextNodes[n.id] = n;
    }
    const nowKnown = new Set([...knownIds, ...Object.keys(nextNodes)]);
    const nextEdges = { ...guestEdgeMap };
    for (const e of ego.edges ?? []) {
      const key = `${e.from}|${e.to}|${e.rel_type}`;
      if (baseEdgeKeys.has(key)) continue;
      if (nowKnown.has(e.from) && nowKnown.has(e.to)) nextEdges[key] = e;
    }
    guestNodesById = nextNodes;
    guestEdgeMap = nextEdges;
  }

  function syncUrl() {
    const url = new URL($page.url);
    url.searchParams.set("scope", scope);
    url.searchParams.set("lens", lens);
    if (focusId) url.searchParams.set("focus", focusId);
    else url.searchParams.delete("focus");
    goto(url, { keepFocus: true, noScroll: true, replaceState: true });
  }
</script>

<div class="explore-page">
  <div class="ex-head">
    <div>
      <a class="eyebrow back-link" href={libraryHref()}>&larr; Library</a>
      <h1 class="grim-title page-title">Explore</h1>
      {#if !graphLoading && !graphError}
        <p class="eyebrow count-line">
          {combinedNodes.length.toLocaleString()} entities &middot; {combinedEdges.length.toLocaleString()}
          relationships
        </p>
      {/if}
    </div>
    <div class="ex-controls">
      <label class="scope-sel">
        <span class="eyebrow">Scope</span>
        <select
          value={scope}
          disabled={adventuresLoading}
          onchange={(e) => onScopeChange(e.currentTarget.value)}
        >
          <option value="everything">Everything</option>
          {#each adventures as adv (adv.id)}
            <option value={`adventure:${adv.id}`}
              >{adv.name} ({adv.book_display_name})</option
            >
          {/each}
        </select>
      </label>
      <div class="lens-switch" role="group" aria-label="Lens">
        {#each LENS_OPTIONS as l (l.value)}
          <button
            type="button"
            class:on={lens === l.value}
            aria-pressed={lens === l.value}
            onclick={() => onLensChange(l.value)}
          >
            {l.label}
          </button>
        {/each}
      </div>
    </div>
  </div>

  <div class="ex-stage">
    {#if graphLoading}
      <div class="stage-loading" aria-hidden="true">
        <div class="skeleton"></div>
      </div>
    {:else if graphError}
      <p class="status-error">{graphError}</p>
    {:else if combinedNodes.length === 0}
      <div class="empty">
        <p class="grim-title empty-lead">Nothing in this view.</p>
        <p class="empty-help">
          Try a different scope or lens &mdash; some slices of the corpus
          don't have entities in every lens yet.
        </p>
      </div>
    {:else}
      <ExploreCanvas
        nodes={combinedNodes}
        edges={combinedEdges}
        {focusId}
        guestIds={guestIdSet}
        onselect={handleSelect}
      />
      {#if legendTypes.length}
        <div class="ex-legend">
          {#each legendTypes as t (t)}
            <div class="legend-row">
              <span
                class="sw"
                aria-hidden="true"
                style={`background: var(--grim-type-${t}, var(--grim-text-faint))`}
              ></span>
              {t.replaceAll("_", " ")}
            </div>
          {/each}
        </div>
      {/if}
    {/if}

    <ExploreCodex
      entityId={focusId}
      onselect={handleSelect}
      onclose={handleClose}
    />
  </div>
</div>

<style>
  .explore-page {
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
    text-decoration: none;
  }

  .back-link:hover {
    color: var(--grim-text-dim);
  }

  .ex-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 20px;
    margin-bottom: 16px;
  }

  .page-title {
    font-size: clamp(32px, 6vw, 46px);
    margin: 4px 0 0;
  }

  .count-line {
    margin-top: 10px;
    font-variant-numeric: tabular-nums;
  }

  .ex-controls {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }

  .scope-sel {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .scope-sel select {
    font-family: var(--grim-serif);
    font-size: 15px;
    font-weight: 600;
    color: var(--grim-ink);
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 8px;
    padding: 8px 12px;
    cursor: pointer;
    max-width: 46vw;
  }

  .scope-sel select:focus-visible {
    border-color: var(--grim-accent);
    box-shadow: 0 0 0 3px var(--grim-accent-soft);
  }

  .lens-switch {
    display: flex;
    gap: 2px;
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    padding: 3px;
  }

  .lens-switch button {
    background: none;
    border: 0;
    cursor: pointer;
    padding: 7px 14px;
    border-radius: 6px;
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    color: var(--grim-text-dim);
  }

  .lens-switch button.on {
    background: var(--grim-surface);
    color: var(--grim-accent);
    box-shadow: 0 1px 2px rgba(20, 30, 50, 0.1);
  }

  .ex-stage {
    position: relative;
    height: calc(100vh - 300px);
    min-height: 440px;
    border: 1px solid var(--grim-line);
    border-radius: 12px;
    overflow: hidden;
    background: var(--grim-surface);
  }

  :global(.grimoire.dark) .ex-stage {
    background: var(--grim-paper);
  }

  .ex-legend {
    position: absolute;
    top: 14px;
    left: 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    background: color-mix(in srgb, var(--grim-surface) 80%, transparent);
    backdrop-filter: blur(8px);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    padding: 11px 13px;
    pointer-events: none;
  }

  .legend-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11.5px;
    color: var(--grim-text-dim);
  }

  .sw {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
  }

  .empty {
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 24px;
  }

  .empty-lead {
    font-size: 26px;
    margin-bottom: 8px;
  }

  .empty-help {
    max-width: 46ch;
    color: var(--grim-text-dim);
  }

  .status-error {
    color: var(--grim-type-creature);
    padding: 32px;
  }

  .stage-loading {
    height: 100%;
    padding: 24px;
  }

  .skeleton {
    height: 100%;
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

  @media (max-width: 640px) {
    .explore-page {
      padding: 28px 20px 60px;
    }
  }
</style>
