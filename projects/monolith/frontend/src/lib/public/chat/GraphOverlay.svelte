<script>
  // The knowledge-graph deep-dive overlay for public chat (ADR 005, Phase 4b).
  //
  // Reuses the exact graph render + note panel from the /notes graph
  // (KnowledgeGraph / NotePanel / GraphSearch / GraphLegend), wrapped in a
  // full-screen brutalist sheet. The nodes a chat turn grounded on (`touched`)
  // are passed straight to the canvas, which paints an accent halo + label on
  // each; clicking any highlighted node expands its body via the browser-direct
  // /notes/body/[id] proxy (NotePanel's `apiBase`), never the chat stream.
  import KnowledgeGraph from "$lib/components/notes/KnowledgeGraph.svelte";
  import NotePanel from "$lib/components/notes/NotePanel.svelte";
  import GraphLegend from "$lib/components/notes/GraphLegend.svelte";
  import GraphSearch from "$lib/components/notes/GraphSearch.svelte";

  /** @type {{
   *   open?: boolean,
   *   nodes?: any[],
   *   edges?: any[],
   *   touched?: { id: any, title: string }[],
   *   focusId?: any,
   *   onClose?: () => void,
   * }} */
  let {
    open = false,
    nodes = [],
    edges = [],
    touched = [],
    focusId = null,
    onClose = () => {},
  } = $props();

  let touchedIds = $derived(new Set(touched.map((n) => n.id)));

  let activeClusters = $state(new Set());
  // Seed the cluster filter from the data once it is present (every type on).
  $effect(() => {
    if (nodes.length && activeClusters.size === 0) {
      activeClusters = new Set(nodes.map((n) => n.type).filter(Boolean));
    }
  });

  let searchTerm = $state("");
  let selectedId = $state(null);
  let zoom = $state(1);
  let hoverTitle = $state("");

  // A chip click in the parent passes a focusId; mirror it into the panel
  // selection so the graph zooms to that node and expands its body.
  $effect(() => {
    if (focusId != null) selectedId = focusId;
  });

  // Clear the selection when the sheet closes so reopening it (via DEEP DIVE)
  // starts from the whole highlighted set rather than a stale note panel.
  $effect(() => {
    if (!open) selectedId = null;
  });

  function toggleCluster(type) {
    const next = new Set(activeClusters);
    next.has(type) ? next.delete(type) : next.add(type);
    activeClusters = next;
  }

  function onKeydown(e) {
    if (e.key === "Escape") onClose();
  }
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
  <div class="overlay" role="dialog" aria-modal="true" aria-label="Knowledge graph deep dive">
    <header class="overlay-bar">
      <div class="overlay-titlewrap">
        <span class="overlay-eyebrow">DEEP DIVE</span>
        <span class="overlay-title">KNOWLEDGE GRAPH</span>
      </div>
      <div class="overlay-meta">
        <span class="overlay-chip overlay-chip--touched">
          {touched.length} TOUCHED
        </span>
        <span class="overlay-chip">{nodes.length} NODES</span>
        <span class="overlay-chip">×{zoom.toFixed(1)}</span>
        <span class="overlay-hover" title={hoverTitle}>
          {hoverTitle || "hover a node"}
        </span>
      </div>
      <button class="overlay-close" onclick={onClose} aria-label="Close deep dive">
        CLOSE ×
      </button>
    </header>

    <div class="overlay-stage">
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
        apiBase="/notes/body"
      />
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 200;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    font-family: var(--mono);
    color: var(--ink);
    animation: overlay-in 180ms ease-out both;
  }
  @keyframes overlay-in {
    from {
      opacity: 0;
      transform: scale(0.99);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }
  .overlay-bar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 18px;
    background: var(--paper);
    border-bottom: 2px solid var(--ink);
    flex-shrink: 0;
  }
  .overlay-titlewrap {
    display: flex;
    flex-direction: column;
    line-height: 1.1;
  }
  .overlay-eyebrow {
    font-size: 9px;
    letter-spacing: 0.18em;
    color: var(--ink-3);
  }
  .overlay-title {
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.08em;
  }
  .overlay-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
    overflow: hidden;
  }
  .overlay-chip {
    font-size: 10px;
    letter-spacing: 0.08em;
    padding: 3px 7px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
    white-space: nowrap;
  }
  .overlay-chip--touched {
    background: var(--accent);
  }
  .overlay-hover {
    font-size: 11px;
    color: var(--ink-3);
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .overlay-close {
    flex-shrink: 0;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 7px 12px;
    border: 2px solid var(--ink);
    background: var(--coral);
    color: var(--ink);
    cursor: pointer;
    box-shadow: var(--shadow-hard-sm);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .overlay-close:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
  }
  .overlay-close:active {
    transform: none;
    box-shadow: none;
  }
  .overlay-stage {
    flex: 1;
    position: relative;
    overflow: hidden;
  }
  @media (max-width: 640px) {
    .overlay-meta {
      gap: 6px;
    }
    .overlay-hover {
      display: none;
    }
    .overlay-chip {
      font-size: 9px;
    }
  }
</style>
