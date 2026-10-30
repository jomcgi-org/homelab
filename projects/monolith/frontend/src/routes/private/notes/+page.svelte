<script>
  import KnowledgeGraph from "$lib/components/notes/KnowledgeGraph.svelte";
  import NotePanel from "$lib/components/notes/NotePanel.svelte";
  import NoteReviewHistory from "$lib/components/notes/NoteReviewHistory.svelte";
  import GraphLegend from "$lib/components/notes/GraphLegend.svelte";
  import GraphSearch from "$lib/components/notes/GraphSearch.svelte";
  import StatusBar from "$lib/components/notes/StatusBar.svelte";
  import "$lib/public/notes/sheet.css";

  let { data } = $props();

  // The server filters nodes by type (see GRAPH_NOTE_TYPES in store.py).
  // We just pass through what arrives — no client-side filter needed.
  let nodes = $derived(data.graph.nodes);
  let edges = $derived(data.graph.edges);
  let indexedAt = $derived(data.graph.indexed_at);

  // Degree is supplied per-node by the server (graph payload).
  let nodesWithDegree = $derived(nodes);

  // activeClusters is initialised from the *initial* graph (a one-time
  // snapshot), then mutated locally as the user toggles. Reading data
  // (a $props value) inside $state() captures cleanly; reading a
  // $derived inside $state() trips state_referenced_locally.
  let activeClusters = $state(
    new Set(data.graph.nodes.map((n) => n.type).filter(Boolean)),
  );
  let searchTerm = $state("");
  let selectedId = $state(null);
  let zoom = $state(1);
  let hoverTitle = $state("—");

  let clusterCount = $derived(activeClusters.size);

  function toggleCluster(type) {
    const next = new Set(activeClusters);
    next.has(type) ? next.delete(type) : next.add(type);
    activeClusters = next;
  }

  function selectNode(id) {
    selectedId = id;
  }
</script>

<div class="td notes-root">
  <StatusBar
    nodeCount={nodes.length}
    edgeCount={edges.length}
    {clusterCount}
    {zoom}
    {hoverTitle}
    {indexedAt}
  />

  <div class="notes-stage sheet-graph">
    <KnowledgeGraph
      sheet
      nodes={nodesWithDegree}
      {edges}
      {selectedId}
      {searchTerm}
      {activeClusters}
      onNodeClick={(e) => selectNode(e.id)}
      onNodeHover={(e) => (hoverTitle = e.title ?? "—")}
      onZoom={(k) => (zoom = k)}
    />

    <GraphSearch value={searchTerm} onChange={(v) => (searchTerm = v)} />
    <GraphLegend
      sheet
      nodes={nodesWithDegree}
      {activeClusters}
      onToggle={toggleCluster}
    />
    <NotePanel
      sheet
      {selectedId}
      nodes={nodesWithDegree}
      {edges}
      onSelect={selectNode}
      onClose={() => (selectedId = null)}
      apiBase="/api/knowledge/notes"
      detailQuery="?include_review_history=true"
    >
      {#snippet detailExtra(detail)}
        <NoteReviewHistory history={detail?.review_history} />
      {/snippet}
    </NotePanel>
  </div>
</div>

<style>
  /* The private graph is the public one's sibling: the same components on
     the same sheet tokens, with the private endpoint behind it. */
  .notes-root {
    height: 100vh;
    display: flex;
    flex-direction: column;
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 13px;
    line-height: 1.45;
  }
  .notes-stage {
    flex: 1;
    position: relative;
    overflow: hidden;
  }
</style>
