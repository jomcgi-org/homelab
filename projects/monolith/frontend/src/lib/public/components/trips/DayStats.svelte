<script>
  // Compact stat row for one day: distance, ascent, descent, photo count.
  let { stats = null, dayColor = "var(--ink)" } = $props();
</script>

{#if stats}
  <div class="stats" style={`--day:${dayColor}`}>
    <div class="cell">
      <span class="label">Distance</span>
      <span class="value">{stats.distance}<span class="unit">km</span></span>
    </div>
    {#if stats.hasElevation}
      <div class="cell">
        <span class="label">Ascent</span>
        <span class="value up">+{stats.ascent.toLocaleString()}<span class="unit">m</span></span>
      </div>
      <div class="cell">
        <span class="label">Descent</span>
        <span class="value down">-{stats.descent.toLocaleString()}<span class="unit">m</span></span>
      </div>
    {/if}
    <div class="cell">
      <span class="label">Photos</span>
      <span class="value">{stats.photoCount ?? 0}</span>
    </div>
  </div>
{/if}

<style>
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
    border: 2px solid var(--ink);
    border-bottom: none;
  }
  .cell {
    padding: 12px 14px;
    border-bottom: 2px solid var(--ink);
    border-right: 2px solid var(--ink);
    background: var(--paper);
  }
  .cell:last-child {
    border-right: none;
  }
  .label {
    display: block;
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 6px;
  }
  .value {
    font-family: var(--mono);
    font-size: 24px;
    font-weight: 900;
    line-height: 1;
    color: var(--ink);
  }
  .unit {
    font-size: 13px;
    font-weight: 700;
    color: var(--ink-3);
    margin-left: 3px;
  }
  .up {
    color: var(--teal);
  }
  .down {
    color: var(--coral);
  }
</style>
