<script>
  let {
    nodeCount,
    edgeCount,
    clusterCount,
    zoom = 1,
    hoverTitle = "—",
    indexedAt = null,
  } = $props();

  function formatAgo(iso) {
    if (!iso) return "—";
    const ms = Date.now() - new Date(iso).getTime();
    const m = Math.floor(ms / 60_000);
    if (m < 1) return "JUST NOW";
    if (m < 60) return `${m}M AGO`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}H AGO`;
    return `${Math.floor(h / 24)}D AGO`;
  }
</script>

<div class="statusbar">
  <div class="statusbar-track">
    <span class="stat">~/KG</span>
    <span class="stat"><strong>{nodeCount.toLocaleString()}</strong> NOTES</span
    >
    <span class="stat"><strong>{edgeCount.toLocaleString()}</strong> LINKS</span
    >
    <span class="stat"><strong>{clusterCount}</strong> CLUSTERS</span>
    <span class="stat">ZOOM <strong>{zoom.toFixed(2)}</strong>×</span>
    <span class="stat">HOVER <strong>{hoverTitle}</strong></span>
    <span class="stat">LAST INDEX <strong>{formatAgo(indexedAt)}</strong></span>
    <span class="stat">~/KG</span>
  </div>
</div>

<style>
  .statusbar {
    position: relative;
    height: 32px;
    background: #f5d90a;
    border-bottom: 1.5px solid #141414;
    overflow: hidden;
    z-index: 4;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    color: #141414;
  }
  .statusbar-track {
    display: flex;
    align-items: center;
    height: 100%;
    white-space: nowrap;
    padding: 0 24px;
    gap: 32px;
    font-size: 11px;
    letter-spacing: 0.06em;
  }
  .stat {
    display: inline-flex;
    align-items: center;
    gap: 10px;
  }
  .stat::before {
    content: "";
    width: 5px;
    height: 5px;
    background: #141414;
    border-radius: 50%;
    display: inline-block;
    margin-right: 4px;
  }
  strong {
    font-weight: 700;
  }
</style>
