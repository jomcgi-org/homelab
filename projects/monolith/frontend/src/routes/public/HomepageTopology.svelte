<script>
  import { browser } from "$app/environment";
  import { fade } from "svelte/transition";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import { DagRenderer, computeLayout } from "$lib/public/components/dag";
  import HomepageNodeDetail from "./HomepageNodeDetail.svelte";

  const FADE_OUT_MS = 180;
  const FADE_IN_MS = 240;

  /** @type {{ topology: object }} */
  let { topology } = $props();

  let hovered = $state(null);
  let hoverTimer = null;

  // Selection is mirrored to the URL (?node=<id>) so the browser back button
  // pops it off naturally. Reading from $page keeps state in one source of
  // truth — no manual sync between local state and URL.
  const selected = $derived($page.url.searchParams.get("node"));

  function setSelected(id) {
    const url = new URL($page.url);
    if (id) url.searchParams.set("node", id);
    else url.searchParams.delete("node");
    goto(url, { keepFocus: true, noScroll: true, replaceState: false });
  }

  function debouncedHover(id) {
    clearTimeout(hoverTimer);
    if (id == null) {
      hoverTimer = setTimeout(() => (hovered = null), 100);
    } else {
      hoverTimer = setTimeout(() => (hovered = id), 100);
    }
  }

  function handleKeydown(e) {
    if (e.key === "Escape" && selected) {
      e.preventDefault();
      setSelected(null);
    }
  }

  // ── Filter for homepage: remove nodes, mark same-rank edges ──
  const excludeIds = new Set(["external"]);
  const sameRankEdges = new Set(["cloudflare->context-forge"]);
  const fullFiltered = $derived.by(() => {
    const groups = (topology.groups || []).filter((g) => !excludeIds.has(g.id));
    const nodes = (topology.nodes || []).filter((n) => !excludeIds.has(n.id));
    const edges = (topology.edges || [])
      .filter((e) => !excludeIds.has(e.from) && !excludeIds.has(e.to))
      .map((e) =>
        sameRankEdges.has(e.from + "->" + e.to) ? { ...e, sameRank: true } : e,
      );
    return { groups, nodes, edges };
  });

  // Subgraph: focused node + neighbors + connecting edges, plus the focused
  // node's group with all its siblings so the group is never drawn half-empty.
  // This also handles infra nodes (which have no top-level edges) by giving
  // them their cluster siblings as visual context instead of a lonely rect.
  const subgraph = $derived.by(() => {
    if (!selected) return null;
    const focusedNode = fullFiltered.nodes.find((n) => n.id === selected);
    if (!focusedNode) return null;

    const matchSet = new Set([selected]);
    if (focusedNode.group) matchSet.add(focusedNode.group);

    const edges = fullFiltered.edges.filter(
      (e) => matchSet.has(e.from) || matchSet.has(e.to),
    );
    const neighborIds = new Set([selected]);
    for (const e of edges) {
      neighborIds.add(e.from);
      neighborIds.add(e.to);
    }

    // Collect every group that contains a node in scope, then pull in all
    // children of those groups so the group renders complete.
    const groupIds = new Set();
    if (focusedNode.group) groupIds.add(focusedNode.group);
    for (const id of neighborIds) {
      const n = fullFiltered.nodes.find((x) => x.id === id);
      if (n?.group) groupIds.add(n.group);
    }
    for (const g of fullFiltered.groups) {
      if (!groupIds.has(g.id)) continue;
      for (const childId of g.children || []) neighborIds.add(childId);
    }

    const nodes = fullFiltered.nodes.filter((n) => neighborIds.has(n.id));
    const groups = fullFiltered.groups.filter((g) => groupIds.has(g.id));
    return { groups, nodes, edges };
  });

  const focusedNode = $derived(
    selected ? fullFiltered.nodes.find((n) => n.id === selected) : null,
  );
  const focusedEdges = $derived(
    selected
      ? fullFiltered.edges.filter((e) => e.from === selected || e.to === selected)
      : [],
  );
  const neighborById = $derived.by(() => {
    const map = new Map();
    for (const n of fullFiltered.nodes) map.set(n.id, n);
    for (const g of fullFiltered.groups) map.set(g.id, g);
    return map;
  });

  const targetRenderable = $derived(subgraph ?? fullFiltered);

  // Layout-input is held back on transition-out so the grid container has
  // time to expand back to full width before the DAG re-flows. Otherwise
  // the DAG snaps to full-graph positions while the pane is still 50%
  // wide — that's the "jerk" on close.
  let renderable = $state(null);
  $effect(() => {
    if (selected) {
      renderable = targetRenderable;
    } else {
      const t = setTimeout(() => {
        renderable = targetRenderable;
      }, 280);
      return () => clearTimeout(t);
    }
  });

  const layout = $derived(
    browser && renderable ? computeLayout(renderable, "LR") : null,
  );
  const groupDefs = $derived(renderable?.groups ?? []);

  const colors = {
    ink: "var(--ink)",
    nodeFill: "#fff",
    nodeText: "var(--ink)",
    groupBorder: "var(--ink)",
    groupLabel: "var(--ink)",
    edge: "var(--ink)",
    arrow: "var(--ink)",
    groupFill: "var(--paper)",
    selectedFill: "var(--accent)",
  };
</script>

<svelte:window onkeydown={handleKeydown} />

<section class="topology-section" id="homelab">
  <div class="topology-wrap" class:split={!!focusedNode}>
    <div class="topology-head">
      <div>
        <p class="eyebrow">Live from the cluster · click a node</p>
        <h2 class="topology-title">Homelab</h2>
      </div>
      <div class="topology-legend mono" aria-label="Node status legend">
        <span class="legend-item"><i class="legend-dot legend-ok"></i>Healthy</span>
        <span class="legend-item"><i class="legend-dot legend-warn"></i>Warning</span>
        <span class="legend-item"><i class="legend-dot legend-bad"></i>Degraded</span>
      </div>
    </div>

    <div class="topology-body" class:split-body={!!focusedNode}>
      <div class="dag-pane">
        {#if layout && renderable && renderable.nodes.length > 0}
          <DagRenderer
            {layout}
            selected={focusedNode ? null : selected}
            {hovered}
            {colors}
            {groupDefs}
            onselect={(id) => setSelected(id === selected ? null : id)}
            onhover={debouncedHover}
          />
        {:else}
          <p class="topology-empty">SLO data unavailable</p>
        {/if}
      </div>

      {#if focusedNode}
        <div
          class="detail-pane"
          in:fade={{ duration: 240 }}
          out:fade={{ duration: 160 }}
        >
          <HomepageNodeDetail
            node={focusedNode}
            edges={focusedEdges}
            {neighborById}
            onClose={() => setSelected(null)}
          />
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  .topology-section {
    background: var(--cream);
    border-bottom: 2px solid var(--ink);
    padding: 64px 0;
    position: relative;
    min-height: 500px;
  }

  .topology-wrap {
    max-width: 1500px;
    margin: 0 auto;
    padding: 0 32px;
  }

  .topology-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }

  .topology-title {
    font-family: var(--mono);
    font-size: clamp(22px, 2.4vw, 32px);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink);
    margin: 4px 0 0;
  }

  .topology-legend {
    display: flex;
    gap: 14px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-2);
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard-sm);
    padding: 8px 12px;
  }

  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .legend-dot {
    width: 9px;
    height: 9px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    display: inline-block;
  }

  .legend-ok {
    background: var(--green);
  }

  .legend-warn {
    background: var(--accent);
  }

  .legend-bad {
    background: var(--coral);
  }

  .topology-empty {
    font-family: var(--mono);
    font-size: 14px;
    color: var(--ink-2);
    text-align: center;
    padding: 120px 0;
  }

  .topology-body {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 0fr);
    gap: 0;
    align-items: start;
    /* Material standard easing — same curve in both directions so the
       container reshape and the panel fade share a consistent rhythm. */
    transition:
      grid-template-columns 280ms cubic-bezier(0.4, 0, 0.2, 1),
      gap 280ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .split-body {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 32px;
  }

  .dag-pane {
    min-width: 0;
  }

  .detail-pane {
    min-width: 0;
  }

  @media (max-width: 900px) {
    .split-body {
      grid-template-columns: 1fr;
      gap: 24px;
    }
  }
</style>
