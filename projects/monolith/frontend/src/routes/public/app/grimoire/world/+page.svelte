<script>
  // Public WORLD: the merged Entities + Explore surface. You pick an entity
  // (search, a graph node, the ?e= URL param, or a dock/chat link) and see its
  // world: the ego neighborhood rendered as a force graph with a docked codex.
  // Clicking a neighbor re-centers on it, seeding shared node positions so the
  // graph settles around the move instead of reshuffling. Scope + lens narrow
  // the ego when set. This is the wandering loop: text -> entity -> graph ->
  // next entity.
  //
  // ssr is off for the whole public grimoire tree (see the root +layout.js
  // docblock): this page fetches everything client-side after the Turnstile
  // gate admits.
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import {
    exploreEgo,
    listEntities,
    listAllAdventures,
    worldHref,
  } from "$lib/public/grimoire/api.js";
  import { constellationStore } from "$lib/public/grimoire/constellation-store.js";
  import ExploreCanvas from "$lib/public/grimoire/explore/ExploreCanvas.svelte";
  import ExploreCodex from "$lib/public/grimoire/explore/ExploreCodex.svelte";
  import EntitySearch from "$lib/public/grimoire/world/EntitySearch.svelte";
  import ScopePicker from "$lib/public/grimoire/world/ScopePicker.svelte";

  const LENS_OPTIONS = [
    { value: "world", label: "World" },
    { value: "story", label: "Story" },
    { value: "quests", label: "Quests" },
    { value: "rules", label: "Rules" },
  ];
  const LENS_VALUES = new Set(LENS_OPTIONS.map((l) => l.value));

  // Rotating landing feature: without a ?e= we land on one of these,
  // day-of-year rotated, resolved to a real entity id below. Names chosen for
  // dense, recognizable neighborhoods (a good first "world" to wander).
  const FEATURED = [
    "Strahd von Zarovich",
    "Tiamat",
    "Waterdeep",
    "Zariel",
    "Acererak",
  ];

  let scope = $state("everything");
  let lens = $state("world");
  let focusId = $state(null);

  let adventures = $state([]);
  let adventuresLoading = $state(true);

  // The current focus's ego neighborhood ({nodes, edges}). Re-fetched on every
  // focus change; the canvas renders this centered on `focusId`.
  let ego = $state({ nodes: [], edges: [] });
  let egoLoading = $state(true);
  let loadError = $state("");

  // Per-lens counts for the current ego, to disable empty lens tabs. Ego
  // responses may not carry lens_counts; default to "all present" so tabs are
  // never wrongly disabled before we know.
  let lensCounts = $state({ world: 1, story: 1, quests: 1, rules: 1 });

  // True when the current `ego` is the unfiltered fallback graph (fetched
  // after a scope/lens combo returned an empty neighborhood), so the UI can
  // say the view was widened. `fellBackFor` is the `focusId|scope|lens` key
  // the fallback already ran for, guarding against re-fetching every time
  // this same empty combination re-renders.
  let fellBackToFullEgo = $state(false);
  let fellBackFor = null;

  // Positions from the previous layout, handed to the canvas so a re-center
  // keeps shared nodes in place (seed) rather than hash-jumping them. The
  // canvas reports its settled positions back after each layout.
  let seedPositions = $state(null);

  // The nodes/edges handed to the canvas. /explore/ego now takes scope/lens
  // and narrows the neighbor set server-side (the focus entity itself is
  // always kept, see explore.ego_subgraph), so `ego` IS the already-scoped
  // graph; `displayGraph` just guards against a missing response shape.
  //
  // A scope/lens combo can legitimately leave a focus entity with zero
  // visible neighbors (e.g. an NPC with no relationships inside the picked
  // adventure). Rather than show a blank canvas, the $effect below falls
  // back to the unfiltered ego (scope=everything, lens=world) and flips
  // `fellBackToFullEgo` so the UI can say so; `fellBackFor` remembers which
  // focus+scope+lens combination the fallback already ran for, so a repeat
  // render of the same empty combo doesn't refetch in a loop.
  const displayGraph = $derived({
    nodes: ego.nodes ?? [],
    edges: ego.edges ?? [],
  });

  const legendTypes = $derived(
    [...new Set(displayGraph.nodes.map((n) => n.entity_type))].sort(),
  );

  // >=900px docks the codex as a right rail; below that it is a bottom sheet.
  let wide = $state(true);

  onMount(() => {
    let cleanup = () => {};
    if (typeof window !== "undefined" && window.matchMedia) {
      const mql = window.matchMedia("(min-width: 900px)");
      wide = mql.matches;
      const onChange = (e) => (wide = e.matches);
      mql.addEventListener("change", onChange);
      cleanup = () => mql.removeEventListener("change", onChange);
    }
    bootstrap();
    return cleanup;
  });

  async function bootstrap() {
    const params = $page.url.searchParams;
    const urlLens = params.get("lens");
    lens = urlLens && LENS_VALUES.has(urlLens) ? urlLens : "world";
    const urlScope = params.get("scope");
    if (urlScope) scope = urlScope;

    loadAdventures();

    const e = params.get("e");
    if (e) {
      focusEntity(e, { push: false });
    } else {
      await landOnFeatured();
    }
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
  }

  // Resolve the rotating featured entity to a real id and land focused with the
  // codex open. Featured names are matched case-insensitively against the
  // search results (exact match preferred, else the first result); if every
  // featured lookup fails, fall back to the most-connected entity (the
  // degree-ordered /entities?limit=1). A totally empty corpus lands unfocused.
  async function landOnFeatured() {
    egoLoading = true;
    const dayOfYear = Math.floor(
      (Date.now() - Date.UTC(new Date().getUTCFullYear(), 0, 0)) / 86_400_000,
    );
    const name = FEATURED[dayOfYear % FEATURED.length];
    const id = (await resolveByName(name)) ?? (await resolveFirstEntity());
    if (id != null) {
      focusEntity(id, { push: false });
    } else {
      egoLoading = false;
    }
  }

  async function resolveByName(name) {
    try {
      const res = await listEntities({ q: name, limit: 5 });
      const items = res.items ?? [];
      if (!items.length) return null;
      const exact = items.find(
        (it) => it.name?.toLowerCase() === name.toLowerCase(),
      );
      return (exact ?? items[0]).id;
    } catch {
      return null;
    }
  }

  async function resolveFirstEntity() {
    try {
      const res = await listEntities({ limit: 1 });
      return (res.items ?? [])[0]?.id ?? null;
    } catch {
      return null;
    }
  }

  // Focus an entity: render it centered, report to the constellation, and
  // (unless bootstrapping from the URL) push ?e= so back/forward and sharing
  // land in the same state. Seeds shared node positions from the current
  // layout so a re-center settles around the move. The actual ego fetch
  // happens in the $effect below, which reacts to `focusId` alongside
  // `scope`/`lens` so every trigger (search, node tap, scope/lens change)
  // goes through one fetch path.
  function focusEntity(id, { push = true } = {}) {
    if (id == null) return;
    // Capture positions of the outgoing layout to seed the incoming one.
    seedPositions = lastPositions;
    focusId = id;
    if (push) syncUrl();
  }

  // Single driver for every ego fetch: reacts to focusId, scope, and lens,
  // so a search/tap re-center and a scope/lens change while focused both
  // funnel through the same path (no separate call sites re-fetching and no
  // double-fetch when focusId changes for other reasons).
  //
  // If the scope/lens-narrowed result has no visible neighbors (a combo can
  // legitimately leave a focus entity isolated, e.g. an NPC with no
  // relationships inside the picked adventure), falls back to the
  // unfiltered ego (scope=everything, lens=world) rather than showing a
  // blank canvas, and sets `fellBackToFullEgo` so the UI can say so.
  // `fellBackFor` remembers the exact focusId+scope+lens key the fallback
  // already ran for, so re-rendering the same empty combo does not refetch.
  //
  // `egoRequestSeq` guards against out-of-order resolution: each effect run
  // takes a ticket, and a run only applies its result (or its error) while
  // its ticket is still the latest, so a slow older fetch can never
  // overwrite the graph of a newer focus/scope/lens.
  let egoRequestSeq = 0;
  $effect(() => {
    const id = focusId;
    const currentScope = scope;
    const currentLens = lens;
    if (id == null) return;
    const requestId = ++egoRequestSeq;
    const comboKey = `${id}|${currentScope}|${currentLens}`;
    egoLoading = true;
    loadError = "";
    (async () => {
      try {
        const res = (await exploreEgo(id, currentScope, currentLens)) ?? {
          nodes: [],
          edges: [],
        };
        if (requestId !== egoRequestSeq) return;
        const isNarrowed =
          currentScope !== "everything" || currentLens !== "world";
        const isEmpty = (res.nodes ?? []).length <= 1;
        if (isNarrowed && isEmpty && fellBackFor !== comboKey) {
          fellBackFor = comboKey;
          const full = (await exploreEgo(id, "everything", "world")) ?? {
            nodes: [],
            edges: [],
          };
          if (requestId !== egoRequestSeq) return;
          applyEgo(id, full);
          fellBackToFullEgo = true;
          return;
        }
        fellBackFor = comboKey;
        fellBackToFullEgo = false;
        applyEgo(id, res);
      } catch (e) {
        if (requestId !== egoRequestSeq) return;
        console.error("Could not load world", e);
        loadError = e.message;
        ego = { nodes: [], edges: [] };
      } finally {
        if (requestId === egoRequestSeq) egoLoading = false;
      }
    })();
  });

  // Shared tail of a successful ego fetch: sets state, records the trail
  // touch, and reports to the constellation. Shared by both the direct
  // fetch and the full-ego fallback above.
  function applyEgo(id, res) {
    ego = res;
    lensCounts = res.lens_counts ?? { world: 1, story: 1, quests: 1, rules: 1 };
    const node = (res.nodes ?? []).find((n) => n.id === id);
    constellationStore.touch({
      id,
      title: node?.name ?? "",
      kind: "entity",
      entity_type: node?.entity_type,
    });
    constellationStore.recordEgo(id, res);
  }

  // The canvas reports its settled node positions here after each layout, so
  // the next re-center can seed shared nodes from where they currently sit.
  let lastPositions = null;
  function onPositions(map) {
    lastPositions = map;
  }

  // A node tap on the canvas re-centers on that node (null = tap empty space,
  // ignored: the codex stays where it is rather than closing on a stray tap).
  function handleSelect(id) {
    if (id == null || id === focusId) return;
    focusEntity(id);
  }

  // A codex relationship click also re-centers.
  function handleCodexSelect(id) {
    if (id == null || id === focusId) return;
    focusEntity(id);
  }

  function handleClose() {
    focusId = null;
    syncUrl();
  }

  function onSearchSelect(id) {
    focusEntity(id);
  }

  function onScopeChange(nextScope) {
    if (nextScope === scope) return;
    scope = nextScope;
    syncUrl();
  }

  function onLensChange(nextLens) {
    if (nextLens === lens || lensCounts[nextLens] === 0) return;
    lens = nextLens;
    syncUrl();
  }

  function syncUrl() {
    const url = new URL($page.url);
    if (focusId != null) url.searchParams.set("e", String(focusId));
    else url.searchParams.delete("e");
    if (scope && scope !== "everything") url.searchParams.set("scope", scope);
    else url.searchParams.delete("scope");
    if (lens && lens !== "world") url.searchParams.set("lens", lens);
    else url.searchParams.delete("lens");
    goto(url, { keepFocus: true, noScroll: true, replaceState: true });
  }
</script>

<div class="world-page">
  <div class="world-head">
    <div class="head-search">
      <EntitySearch onselect={onSearchSelect} />
    </div>
    <div class="head-controls">
      <ScopePicker
        {adventures}
        value={scope}
        disabled={adventuresLoading}
        onchange={onScopeChange}
      />
      <div class="lens-switch" role="group" aria-label="Lens">
        {#each LENS_OPTIONS as l (l.value)}
          <button
            type="button"
            class:on={lens === l.value}
            aria-pressed={lens === l.value}
            disabled={lensCounts[l.value] === 0}
            onclick={() => onLensChange(l.value)}
          >
            {l.label}
          </button>
        {/each}
      </div>
    </div>
  </div>

  <div class="world-main" class:wide>
    <div class="stage">
      {#if egoLoading && displayGraph.nodes.length === 0}
        <div class="stage-loading" aria-hidden="true">
          <div class="skeleton"></div>
        </div>
      {:else if loadError && displayGraph.nodes.length === 0}
        <p class="status-error">
          Could not load this right now. Try again in a moment.
        </p>
      {:else if displayGraph.nodes.length === 0}
        <div class="empty">
          <p class="grim-title empty-lead">Nothing to show.</p>
          <p class="empty-help">
            Search for a person or place to see their world.
          </p>
        </div>
      {:else}
        <ExploreCanvas
          nodes={displayGraph.nodes}
          edges={displayGraph.edges}
          {focusId}
          initialPositions={seedPositions}
          onselect={handleSelect}
          onpositions={onPositions}
        />
        {#if fellBackToFullEgo}
          <p class="fallback-note">
            Nothing connects to this in the chosen book and view. Showing the
            whole world instead.
          </p>
        {/if}
        {#if legendTypes.length}
          <div class="legend">
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
    </div>

    <div class="codex-dock" class:open={!!focusId}>
      <ExploreCodex
        entityId={focusId}
        onselect={handleCodexSelect}
        onclose={handleClose}
      />
    </div>
  </div>
</div>

<style>
  /* Flex column filling the viewport under the 58px sticky topbar; the main
     region flexes to the bottom edge (min-height:0 lets the canvas region
     actually shrink to fit instead of overflowing, fixing the old
     bottom-cutoff). */
  .world-page {
    display: flex;
    flex-direction: column;
    min-height: calc(100dvh - 58px);
    height: calc(100dvh - 58px);
  }

  .world-head {
    flex: none;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 24px;
    border-bottom: 1px solid var(--grim-line);
  }

  .head-search {
    max-width: 640px;
  }

  .head-controls {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
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

  .lens-switch button:disabled {
    color: var(--grim-text-faint);
    opacity: 0.5;
    cursor: not-allowed;
  }

  .lens-switch button:not(:disabled):hover {
    color: var(--grim-ink);
  }

  .world-main {
    flex: 1 1 auto;
    min-height: 0;
    position: relative;
    display: flex;
  }

  .stage {
    position: relative;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    background: var(--grim-surface);
    overflow: hidden;
  }

  /* >=900px: codex docked as a right rail, sharing the row with the canvas.
     Below that it becomes a bottom sheet overlaying the canvas. */
  .codex-dock {
    position: relative;
    flex: none;
  }

  .world-main.wide .codex-dock {
    width: 0;
    border-left: 0;
    overflow: hidden;
    transition: width 0.3s cubic-bezier(0.22, 0.61, 0.36, 1);
  }

  .world-main.wide .codex-dock.open {
    width: 360px;
    border-left: 1px solid var(--grim-line);
  }

  @media (prefers-reduced-motion: reduce) {
    .world-main.wide .codex-dock {
      transition: none;
    }
  }

  .world-main.wide .codex-dock :global(.codex) {
    /* On wide screens the codex fills its rail statically rather than sliding
       over the canvas: reset the absolute slide-in positioning. */
    position: relative;
    width: 100%;
    border-left: 0;
    transform: none;
    backdrop-filter: none;
    background: var(--grim-surface);
    height: 100%;
  }

  /* Under 900px the codex keeps its own slide-in (absolute, from the right)
     from ExploreCodex, but anchored to the bottom as a sheet. */
  .world-main:not(.wide) .codex-dock {
    position: static;
  }

  .world-main:not(.wide) .codex-dock :global(.codex) {
    position: fixed;
    top: auto;
    bottom: 0;
    left: 0;
    right: 0;
    width: 100%;
    height: min(70dvh, 560px);
    border-left: 0;
    border-top: 1px solid var(--grim-line);
    transform: translateY(100%);
  }

  .world-main:not(.wide) .codex-dock :global(.codex.open) {
    transform: translateY(0);
  }

  .fallback-note {
    position: absolute;
    top: 14px;
    right: 14px;
    max-width: 32ch;
    background: color-mix(in srgb, var(--grim-surface) 80%, transparent);
    backdrop-filter: blur(8px);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    padding: 8px 12px;
    font-size: 11.5px;
    color: var(--grim-text-dim);
    pointer-events: none;
  }

  .legend {
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
    .world-head {
      padding: 14px 16px;
    }
  }
</style>
