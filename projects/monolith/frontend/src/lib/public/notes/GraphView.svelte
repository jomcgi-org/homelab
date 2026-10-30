<script>
  // The in-page knowledge-graph view for /app/notes (ADR 005, Phase 4b).
  //
  // Built as the graph half of the notes chat's chat-vs-graph toggle; the chat
  // is retired (#6913) and this is now the whole /app/notes page. The page
  // mounts it in the browser only (and, through it, the heavy KnowledgeGraph
  // canvas + d3 layout), and the graph DATA is fetched here (on mount) from the
  // same-origin /app/notes/graph proxy, so the SSR shell stays light.
  //
  // Reuses the exact graph render + note panel from the standalone graph
  // (KnowledgeGraph / NotePanel / GraphSearch / GraphLegend). An optional
  // `touched` set is passed to the canvas as `touchedIds`, which paints an
  // accent halo + label on each; clicking any node expands its body via the
  // browser-direct /app/notes/body/[id] proxy (NotePanel's `apiBase`).
  import { onMount } from "svelte";
  import { Button } from "@homelab/design-system/components";
  import "./sheet.css";
  import KnowledgeGraph from "$lib/components/notes/KnowledgeGraph.svelte";
  import NotePanel from "$lib/components/notes/NotePanel.svelte";
  import GraphLegend from "$lib/components/notes/GraphLegend.svelte";
  import GraphSearch from "$lib/components/notes/GraphSearch.svelte";
  import {
    initialGraphSelection,
    selectionForFocus,
  } from "$lib/public/notes/graph-selection.js";
  import {
    getCachedGraph,
    setCachedGraph,
  } from "$lib/public/notes/graph-cache.js";

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

  // A deep link (?focus=<note id>, used by the factory context viewer) passes
  // a focusId; mirror it into the panel selection so the graph zooms to that
  // node and expands its body. A null focusId leaves the panel empty.
  $effect(() => {
    const sel = selectionForFocus(focusId);
    if (sel != null) selectedId = sel;
  });

  let expiryTimer;
  let stopped = false;
  let pending = false;

  function expireAt(expiresAt) {
    clearTimeout(expiryTimer);
    expiryTimer = setTimeout(
      () => {
        nodes = [];
        edges = [];
        status = "loading";
        if (!document.hidden) void loadGraph();
      },
      Math.max(0, expiresAt - Date.now()),
    );
  }

  async function loadGraph() {
    if (pending || stopped) return;
    // Reuse only a still-fresh parsed snapshot; HTTP age survives remounts.
    const cached = getCachedGraph();
    if (cached) {
      nodes = cached.nodes;
      edges = cached.edges;
      status = "ready";
      expireAt(cached.expiresAt);
      return;
    }
    status = "loading";
    pending = true;
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
      const expiresAt = setCachedGraph({ nodes, edges }, res.headers);
      if (stopped) return;
      status = "ready";
      if (expiresAt <= Date.now()) {
        nodes = [];
        edges = [];
        status = graph.nodes?.length ? "error" : "ready";
      } else expireAt(expiresAt);
    } catch {
      if (!stopped) status = "error";
    } finally {
      pending = false;
    }
  }

  onMount(() => {
    void loadGraph();
    const visible = () => {
      if (!document.hidden && !getCachedGraph()) void loadGraph();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      stopped = true;
      clearTimeout(expiryTimer);
      document.removeEventListener("visibilitychange", visible);
    };
  });

  function toggleCluster(type) {
    const next = new Set(activeClusters);
    next.has(type) ? next.delete(type) : next.add(type);
    activeClusters = next;
  }
</script>

<div class="graph-view sheet-graph">
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
        <Button variant="secondary" onclick={loadGraph}>TRY AGAIN</Button>
      </div>
    {:else}
      <KnowledgeGraph
        sheet
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
      <GraphLegend sheet {nodes} {activeClusters} onToggle={toggleCluster} />
      <NotePanel
        sheet
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
    background: var(--sheet);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    /* The page supplies an explicit graph height inside its edition. */
    flex: 1;
    min-height: 0;
    height: 100%;
    font-family: var(--font-code);
    color: var(--ink);
  }
  .graph-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    background: var(--band);
    border-bottom: 1px solid var(--stroke);
    flex-shrink: 0;
  }
  .graph-chip {
    font-size: 10px;
    letter-spacing: 0.08em;
    padding: 3px 7px;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    white-space: nowrap;
  }
  .graph-chip--touched {
    color: var(--accent-ink);
  }
  .graph-hover {
    font-size: 11px;
    color: var(--ink-2);
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
    color: var(--ink-2);
  }
  .graph-thinking .dot {
    width: 5px;
    height: 5px;
    background: var(--ink-2);
    display: inline-block;
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
  @media (prefers-reduced-motion: no-preference) {
    .graph-thinking .dot {
      animation: graph-dot-pulse 1s ease-in-out infinite;
    }
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
