<script>
  import KnowledgeGraph from "$lib/components/notes/KnowledgeGraph.svelte";
  import NotePanel from "$lib/components/notes/NotePanel.svelte";
  import GraphLegend from "$lib/components/notes/GraphLegend.svelte";
  import GraphSearch from "$lib/components/notes/GraphSearch.svelte";
  import StatusBar from "$lib/components/notes/StatusBar.svelte";
  import NotesChat from "$lib/components/notes/NotesChat.svelte";

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

<div class="notes-root">
  <StatusBar
    nodeCount={nodes.length}
    edgeCount={edges.length}
    {clusterCount}
    {zoom}
    {hoverTitle}
    {indexedAt}
  />

  <div class="notes-stage">
    <KnowledgeGraph
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
      nodes={nodesWithDegree}
      {activeClusters}
      onToggle={toggleCluster}
    />
    <NotePanel
      {selectedId}
      nodes={nodesWithDegree}
      {edges}
      onSelect={selectNode}
      onClose={() => (selectedId = null)}
      apiBase="/api/knowledge/notes"
    />
    <NotesChat apiBase="/api/knowledge" isPublic={false} />
  </div>
</div>

<style>
  .notes-root {
    height: calc(100vh - 48px);
    display: flex;
    flex-direction: column;
    background: #f1ebdc;
    color: #141414;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 13px;
    line-height: 1.45;
  }
  .notes-stage {
    flex: 1;
    position: relative;
    overflow: hidden;
  }
</style>
