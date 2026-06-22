<script>
  // The in-page knowledge-graph view for public chat (ADR 005, Phase 4b).
  //
  // This is the lazy half of the /app/notes chat-vs-graph toggle: the page
  // dynamically imports this component (and, through it, the heavy
  // KnowledgeGraph canvas + d3 layout) only when the visitor switches to the
  // graph view, so the initial chat load stays light. The graph DATA is fetched
  // here too (on mount) from the same-origin /app/notes/graph proxy, so nothing
  // is loaded until the view is opened.
  //
  // Reuses the exact graph render + note panel from the standalone graph
  // (KnowledgeGraph / NotePanel / GraphSearch / GraphLegend). The nodes a chat
  // turn grounded on (`touched`) are passed to the canvas as `touchedIds`, which
  // paints an accent halo + label on each; clicking any node expands its body
  // via the browser-direct /app/notes/body/[id] proxy (NotePanel's `apiBase`),
  // never the chat stream.
  import { onMount } from "svelte";
  import KnowledgeGraph from "$lib/components/notes/KnowledgeGraph.svelte";
  import NotePanel from "$lib/components/notes/NotePanel.svelte";
  import GraphLegend from "$lib/components/notes/GraphLegend.svelte";
  import GraphSearch from "$lib/components/notes/GraphSearch.svelte";
  import {
    initialGraphSelection,
    selectionForFocus,
  } from "$lib/public/chat/chat-state.js";
  import {
    getCachedGraph,
    setCachedGraph,
  } from "$lib/public/chat/graph-cache.js";

  /** @type {{
   *   touched?: { id: any, title: string }[],
   *   focusId?: any,
   * }} */
  let { touched = [], focusId = null } = $props();

  let touchedIds = $derived(new Set(touched.map((n) => n.id)));

  // Graph payload, fetched lazily on mount from the same-origin proxy. The
  // browser never calls the backend directly (the gateway only routes to the
  // frontend); /app/notes/graph forwards to the visibility-filtered public
  // endpoint server-side.
  let nodes = $state([]);
  let edges = $state([]);
  let status = $state("loading"); // "loading" | "ready" | "error"

  let activeClusters = $state(new Set());
  // Seed the cluster filter from the data once it is present (every type on).
  $effect(() => {
    if (nodes.length && activeClusters.size === 0) {
      activeClusters = new Set(nodes.map((n) => n.type).filter(Boolean));
    }
  });

  let searchTerm = $state("");
  // Nothing is selected on entry: a fresh graph shows the "hover a node"
  // placeholder, never an auto-selected note (ADR 005, Phase 4 polish).
  let selectedId = $state(initialGraphSelection());
  let zoom = $state(1);
  let hoverTitle = $state("");

  // A chip click on the chat side passes a focusId; mirror it into the panel
  // selection so the graph zooms to that node and expands its body. A null
  // focusId (the GRAPH tab / DEEP DIVE) leaves the panel empty.
  $effect(() => {
    const sel = selectionForFocus(focusId);
    if (sel != null) selectedId = sel;
  });

  async function loadGraph() {
    // Toggling Chat <-> Graph remounts this component. Reuse the parsed graph
    // from a prior open in this page session instead of re-fetching and
    // re-parsing ~3.4 MB every time (the HTTP layer's s-maxage/ETag still
    // governs freshness across full page loads, when the module cache resets).
    const cached = getCachedGraph();
    if (cached) {
      nodes = cached.nodes;
      edges = cached.edges;
      status = "ready";
      return;
    }
    status = "loading";
    try {
      const res = await fetch("/app/notes/graph", {
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) {
        status = "error";
        return;
      }
      const graph = await res.json();
      nodes = graph.nodes ?? [];
      edges = graph.edges ?? [];
      setCachedGraph({ nodes, edges });
      status = "ready";
    } catch {
      status = "error";
    }
  }

  onMount(loadGraph);

  function toggleCluster(type) {
    const next = new Set(activeClusters);
    next.has(type) ? next.delete(type) : next.add(type);
    activeClusters = next;
  }
</script>

<div class="graph-view">
  <header class="graph-bar">
    <span class="graph-chip graph-chip--touched">{touched.length} TOUCHED</span>
    <span class="graph-chip">{nodes.length} NODES</span>
    <span class="graph-chip">×{zoom.toFixed(1)}</span>
    <span class="graph-hover" title={hoverTitle}>
      {hoverTitle || "hover a node"}
    </span>
  </header>

  <div class="graph-stage">
    {#if status === "loading"}
      <div class="graph-state">
        <p class="graph-thinking">
          <span class="dot"></span><span class="dot"></span><span class="dot"
          ></span>
          loading the graph
        </p>
      </div>
    {:else if status === "error"}
      <div class="graph-state">
        <p class="graph-error-copy">The graph is unavailable right now.</p>
        <button type="button" class="graph-retry" onclick={loadGraph}>
          TRY AGAIN
        </button>
      </div>
    {:else}
      <KnowledgeGraph
        {nodes}
        {edges}
        {selectedId}
        {searchTerm}
        {activeClusters}
        {touchedIds}
        onNodeClick={(e) => (selectedId = e.id)}
        onNodeHover={(e) => (hoverTitle = e.title ?? "")}
        onZoom={(k) => (zoom = k)}
      />

      <GraphSearch value={searchTerm} onChange={(v) => (searchTerm = v)} />
      <GraphLegend {nodes} {activeClusters} onToggle={toggleCluster} />
      <NotePanel
        {selectedId}
        {nodes}
        {edges}
        onSelect={(id) => (selectedId = id)}
        onClose={() => (selectedId = null)}
        apiBase="/app/notes/body"
      />
      <!-- No empty-state box on the default view: the toolbar already shows the
           "hover a node" hint, and NotePanel overlays only once a node is
           clicked, so the default view is just the graph canvas + search +
           legend (no shadowed floating box). -->
    {/if}
  </div>
</div>

<style>
  .graph-view {
    border: 2px solid var(--ink);
    background: var(--paper);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    /* Fills the full-screen view-area on /app/notes (the parent makes this a
       flex:1 item); the height:100% resolves against that flex height. */
    flex: 1;
    min-height: 0;
    height: 100%;
    font-family: var(--mono);
    color: var(--ink);
  }
  .graph-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    background: var(--paper);
    border-bottom: 2px solid var(--ink);
    flex-shrink: 0;
  }
  .graph-chip {
    font-size: 10px;
    letter-spacing: 0.08em;
    padding: 3px 7px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
    white-space: nowrap;
  }
  .graph-chip--touched {
    background: var(--accent);
  }
  .graph-hover {
    font-size: 11px;
    color: var(--ink-3);
    margin-left: auto;
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .graph-stage {
    flex: 1;
    position: relative;
    overflow: hidden;
  }
  .graph-state {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
  }
  .graph-thinking {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--ink-3);
  }
  .graph-thinking .dot {
    width: 5px;
    height: 5px;
    background: var(--ink-3);
    display: inline-block;
    animation: graph-dot-pulse 1s ease-in-out infinite;
  }
  .graph-thinking .dot:nth-child(2) {
    animation-delay: 0.15s;
  }
  .graph-thinking .dot:nth-child(3) {
    animation-delay: 0.3s;
  }
  @keyframes graph-dot-pulse {
    0%,
    100% {
      opacity: 0.3;
    }
    50% {
      opacity: 1;
    }
  }
  .graph-error-copy {
    font-size: 13px;
    color: var(--ink-2);
  }
  .graph-retry {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 7px 12px;
    border: 2px solid var(--ink);
    background: var(--accent);
    cursor: pointer;
  }

  /* Flatten the reused graph chrome within this view (the shared components
     carry a hard 4px offset shadow); scoped here so the private notes graph is
     untouched and NotePanel does not need editing. */
  .graph-view :global(.search),
  .graph-view :global(.legend),
  .graph-view :global(.panel) {
    box-shadow: none;
  }
  /* Whiten the graph canvas: the shared stage is a cream parchment that reads as
     cream-on-cream against the page. Scoped so the private notes graph keeps its
     parchment. */
  .graph-view :global(.stage) {
    background: var(--paper);
  }

  @media (max-width: 640px) {
    .graph-hover {
      display: none;
    }
    .graph-chip {
      font-size: 9px;
    }
  }
</style>
