<script>
  import { colorFor, labelFor } from "./clusters.js";

  let { nodes, activeClusters, onToggle } = $props();

  let counts = $derived(
    nodes.reduce((acc, n) => {
      const k = n.type ?? "other";
      acc[k] = (acc[k] ?? 0) + 1;
      return acc;
    }, {}),
  );
  let entries = $derived(Object.entries(counts).sort((a, b) => b[1] - a[1]));
</script>

<div class="legend">
  <h4>VAULT</h4>
  {#each entries as [type, count]}
    <div
      class="legend-row"
      class:off={!activeClusters.has(type)}
      onclick={() => onToggle(type)}
      onkeydown={(e) => {
        if (e.key === "Enter" || e.key === " ") onToggle(type);
      }}
      role="button"
      tabindex="0"
    >
      <span class="left">
        <span class="swatch" style:background={colorFor(type)}></span>
        <span class="name">{labelFor(type)}</span>
      </span>
      <span class="count">{count.toLocaleString()}</span>
    </div>
  {/each}
</div>

<style>
  .legend {
    position: absolute;
    left: 20px;
    bottom: 20px;
    background: #ffffff;
    border: 1.5px solid #141414;
    box-shadow: 4px 4px 0 #141414;
    padding: 12px 14px;
    min-width: 200px;
    z-index: 5;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    color: #141414;
  }
  h4 {
    margin: 0 0 8px;
    font-size: 9px;
    letter-spacing: 0.14em;
    font-weight: 700;
  }
  .legend-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    font-size: 11px;
    padding: 3px 0;
    cursor: pointer;
    user-select: none;
  }
  .legend-row:hover {
    color: #ff6b5b;
  }
  .legend-row.off {
    opacity: 0.35;
    text-decoration: line-through;
  }
  .legend-row .swatch {
    width: 12px;
    height: 12px;
    border: 1.5px solid #141414;
    flex-shrink: 0;
  }
  .legend-row .name {
    flex: 1;
  }
  .legend-row .count {
    font-size: 10px;
    color: #8a857a;
  }
  .legend-row .left {
    display: flex;
    align-items: center;
    gap: 8px;
  }
</style>
