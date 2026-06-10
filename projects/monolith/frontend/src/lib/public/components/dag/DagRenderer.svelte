<script>
  const HH = 14;

  let {
    layout,
    selected = null,
    hovered = null,
    colors = {},
    groupDefs = [],
    onselect = null,
    onhover = null,
  } = $props();

  const c = $derived({
    ink: colors.ink ?? "#1a1a1a",
    nodeFill: colors.nodeFill ?? "#fef08a",
    nodeText: colors.nodeText ?? "#1a1a1a",
    groupBorder: colors.groupBorder ?? "rgba(26, 26, 26, 0.15)",
    groupLabel: colors.groupLabel ?? "#555",
    edge: colors.edge ?? "#7a7a7a",
    arrow: colors.arrow ?? "#7a7a7a",
    groupFill: colors.groupFill ?? "#f5f0e8",
    selectedFill: colors.selectedFill ?? "var(--accent)",
  });

  const childToGroup = $derived.by(() => {
    const m = {};
    for (const g of groupDefs) {
      for (const cid of g.children) m[cid] = g.id;
    }
    return m;
  });

  const active = $derived(hovered || selected);

  const viewBox = $derived.by(() => {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of layout.nodes) {
      minX = Math.min(minX, n.x - n.hw - 20);
      minY = Math.min(minY, n.y - HH - 20);
      maxX = Math.max(maxX, n.x + n.hw + 20);
      maxY = Math.max(maxY, n.y + HH + 20);
    }
    for (const g of layout.groups || []) {
      minX = Math.min(minX, g.bounds.minX - 10);
      minY = Math.min(minY, g.bounds.minY - 10);
      maxX = Math.max(maxX, g.bounds.maxX + 10);
      maxY = Math.max(maxY, g.bounds.maxY + 10);
    }
    const pad = 40;
    return `${minX - pad} ${minY - pad} ${maxX - minX + pad * 2} ${maxY - minY + pad * 2}`;
  });

  function boxExit(cx, cy, hw, hh, tx, ty) {
    const dx = tx - cx;
    const dy = ty - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    const sx = dx !== 0 ? hw / Math.abs(dx) : Infinity;
    const sy = dy !== 0 ? hh / Math.abs(dy) : Infinity;
    const s = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
  }

  function isHighlighted(nodeId) {
    const src = hovered || selected;
    if (!src) return true;
    if (nodeId === src) return true;
    const srcGroup = groupDefs.find((g) => g.id === src);
    if (srcGroup) {
      if (srcGroup.children.includes(nodeId)) return true;
      const childSet = new Set(srcGroup.children);
      for (const e of layout.edges) {
        if (childSet.has(e.from) && e.to === nodeId) return true;
        if (childSet.has(e.to) && e.from === nodeId) return true;
      }
      const grp = groupDefs.find((g) => g.id === nodeId);
      if (grp) {
        for (const cid of grp.children) {
          for (const e of layout.edges) {
            if (childSet.has(e.from) && e.to === cid) return true;
            if (childSet.has(e.to) && e.from === cid) return true;
          }
        }
      }
    }
    if (childToGroup[src] === nodeId) return true;
    for (const e of layout.edges) {
      if (e.from === src && e.to === nodeId) return true;
      if (e.to === src && e.from === nodeId) return true;
      const grp = groupDefs.find((g) => g.id === nodeId);
      if (grp) {
        if (e.from === src && grp.children.includes(e.to)) return true;
        if (e.to === src && grp.children.includes(e.from)) return true;
      }
    }
    return false;
  }

  function isEdgeHighlighted(from, to) {
    const src = hovered || selected;
    if (!src) return true;
    if (from === src || to === src) return true;
    const srcGroup = groupDefs.find((g) => g.id === src);
    if (srcGroup) {
      const childSet = new Set(srcGroup.children);
      if (childSet.has(from) || childSet.has(to)) return true;
    }
    return false;
  }

  // Compute edge paths with arrowheads
  const edgePaths = $derived.by(() => {
    const { nodeById, groupById, edges } = layout;
    return edges.map((e) => {
      const fromPos = nodeById[e.from] || groupById[e.from];
      const toPos = nodeById[e.to] || groupById[e.to];
      if (!fromPos || !toPos) return null;
      const fromIsGroup = !!groupById[e.from];
      const toIsGroup = !!groupById[e.to];
      const p1 = boxExit(fromPos.x, fromPos.y, fromIsGroup ? fromPos.hw : fromPos.hw + 11, fromIsGroup ? fromPos.hh : HH + 4, toPos.x, toPos.y);
      const p2 = boxExit(toPos.x, toPos.y, toIsGroup ? toPos.hw : toPos.hw + 11, toIsGroup ? toPos.hh : HH + 4, fromPos.x, fromPos.y);

      // Arrowhead at p2 (forward)
      const dx = p2.x - p1.x;
      const dy = p2.y - p1.y;
      const len = Math.sqrt(dx * dx + dy * dy);
      let fwdArrow = "";
      if (len > 0) {
        const ux = dx / len, uy = dy / len;
        const px = -uy, py = ux;
        const size = 8, spread = 4;
        fwdArrow = `M${p2.x - ux * size + px * spread},${p2.y - uy * size + py * spread} L${p2.x},${p2.y} L${p2.x - ux * size - px * spread},${p2.y - uy * size - py * spread}`;
      }

      // Reverse arrowhead at p1 (if bidi)
      let revArrow = "";
      if (e.bidi && len > 0) {
        const ux = -dx / len, uy = -dy / len;
        const px = -uy, py = ux;
        const size = 8, spread = 4;
        revArrow = `M${p1.x - ux * size + px * spread},${p1.y - uy * size + py * spread} L${p1.x},${p1.y} L${p1.x - ux * size - px * spread},${p1.y - uy * size - py * spread}`;
      }

      return { ...e, p1, p2, fwdArrow, revArrow };
    }).filter(Boolean);
  });
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<svg
  {viewBox}
  class="map"
  role="img"
  aria-label="Service topology"
  preserveAspectRatio="xMidYMin meet"
  onclick={(ev) => { if (ev.target.tagName === 'svg') onselect?.(null); }}
>
  <!-- Groups -->
  {#each layout.groups || [] as grp}
    {@const hi = isHighlighted(grp.id)}
    {@const isSelected = active === grp.id}
    {@const labelW = grp.label.length * 6.5 + 20}
    {@const labelH = 22}
    {@const gx = grp.bounds.minX}
    {@const gy = grp.bounds.minY}
    {@const gw = grp.bounds.maxX - grp.bounds.minX}
    {@const gh = grp.bounds.maxY - grp.bounds.minY}
    {@const sw = isSelected ? 3 : 2}
    {@const isActive = active === grp.id}
    <g class="dag-group" style:opacity={hi ? 1 : 0.15} style:transition="opacity 0.4s ease">
      <!-- Base fill lifts groups off the cream background -->
      {#if c.groupFill !== "none"}
        <rect
          x={gx} y={gy}
          width={gw} height={gh}
          fill={c.groupFill}
        />
      {/if}
      <!-- Group fill on hover/select -->
      {#if isActive}
        <rect
          x={gx} y={gy}
          width={gw} height={gh}
          fill="var(--blue)"
        />
      {/if}
      <!-- Group boundary + label tab as one continuous path -->
      <path
        d="M{gx + labelW},{gy} L{gx + gw},{gy} L{gx + gw},{gy + gh} L{gx},{gy + gh} L{gx},{gy + labelH} L{gx + labelW},{gy + labelH} L{gx + labelW},{gy}"
        fill="none"
        stroke={c.groupBorder}
        stroke-width={sw}
        stroke-dasharray="5 3"
      />
      <!-- White fill behind label -->
      <rect
        x={gx} y={gy}
        width={labelW} height={labelH}
        fill="#fff"
        stroke="none"
      />
      <!-- Top and left edge of label tab (shared with group) -->
      <path
        d="M{gx},{gy + labelH} L{gx},{gy} L{gx + labelW},{gy}"
        fill="none"
        stroke={c.groupBorder}
        stroke-width={sw}
        stroke-dasharray="5 3"
      />
      <text
        x={gx + 10} y={gy + labelH / 2 + 3}
        class="group-label"
        fill={c.groupLabel}
      >
        {grp.label}
      </text>
    </g>
  {/each}

  <!-- Edges -->
  {#each edgePaths as e}
    {@const hi = isEdgeHighlighted(e.from, e.to)}
    <g style:opacity={hi ? 1 : 0.08} style:transition="opacity 0.4s ease">
      <line
        x1={e.p1.x} y1={e.p1.y}
        x2={e.p2.x} y2={e.p2.y}
        stroke={c.edge}
        stroke-width="2"
      />
      {#if e.fwdArrow}
        <path d={e.fwdArrow} fill="none" stroke={c.arrow} stroke-width="2" />
      {/if}
      {#if e.revArrow}
        <path d={e.revArrow} fill="none" stroke={c.arrow} stroke-width="2" />
      {/if}
    </g>
  {/each}

  <!-- Nodes -->
  {#each layout.nodes as n}
    {@const pos = layout.nodeById[n.id]}
    {@const w = n.hw * 2 + 22}
    {@const h = HH * 2 + 6}
    {@const hi = isHighlighted(n.id)}
    {@const isSelected = active === n.id}
    {@const dotFill = n.status === "degraded"
      ? "var(--coral, #ff7169)"
      : n.status === "warning"
        ? "var(--accent, #ffde01)"
        : "var(--green, #4ade80)"}
    {#if pos}
      <g class="dag-node" style:opacity={hi ? 1 : 0.15} style:transition="opacity 0.4s ease">
        <!-- Hard offset shadow — the sticker motif carried into the DAG -->
        <rect
          x={pos.x - w / 2 + 3} y={pos.y - h / 2 + 3}
          width={w} height={h}
          fill={c.ink}
        />
        <rect
          x={pos.x - w / 2} y={pos.y - h / 2}
          width={w} height={h}
          fill={isSelected ? c.selectedFill : c.nodeFill}
          stroke={c.ink}
          stroke-width="2"
        />
        <circle
          cx={pos.x - w / 2 + 10}
          cy={pos.y}
          r="4"
          fill={dotFill}
          stroke={c.ink}
          stroke-width="1.5"
        />
        <text
          x={pos.x - w / 2 + 22} y={pos.y + 4}
          class="node-label"
          class:node-label--active={isSelected}
          fill={c.nodeText}
        >
          {n.label}
        </text>
      </g>
    {/if}
  {/each}

  <!-- Hit areas: groups -->
  {#each layout.groups || [] as grp}
    <rect
      x={grp.bounds.minX} y={grp.bounds.minY}
      width={grp.bounds.maxX - grp.bounds.minX}
      height={grp.bounds.maxY - grp.bounds.minY}
      fill="transparent"
      class="hit-area"
      role="button"
      tabindex="0"
      aria-label="{grp.label} group"
      onclick={() => onselect?.(selected === grp.id ? null : grp.id)}
      onmouseenter={() => onhover?.(grp.id)}
      onmouseleave={() => onhover?.(null)}
      onfocus={() => onhover?.(grp.id)}
      onblur={() => onhover?.(null)}
    />
  {/each}

  <!-- Hit areas: nodes -->
  {#each layout.nodes as n}
    {@const pos = layout.nodeById[n.id]}
    {@const w = n.hw * 2 + 22}
    {#if pos}
      <rect
        x={pos.x - w / 2} y={pos.y - HH - 3}
        width={w} height={HH * 2 + 6}
        fill="transparent"
        class="hit-area"
        role="button"
        tabindex="0"
        aria-label={n.label}
        onclick={() => onselect?.(selected === n.id ? null : n.id)}
        onmouseenter={() => onhover?.(n.id)}
        onmouseleave={() => onhover?.(null)}
        onfocus={() => onhover?.(n.id)}
        onblur={() => onhover?.(null)}
      />
    {/if}
  {/each}
</svg>

<style>
  .map {
    width: 100%;
    height: 100%;
  }

  .node-label {
    font-size: 9px;
    font-weight: 800;
    text-anchor: start;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .group-label {
    font-size: 8px;
    font-weight: 700;
    text-anchor: start;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }

  .node-label--active { text-decoration: underline; }

  .hit-area {
    outline: none;
    cursor: pointer;
  }
</style>
